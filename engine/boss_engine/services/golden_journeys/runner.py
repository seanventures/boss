"""Run a golden journey against an install's API and judge it with the Acceptance Agent's verdict.

THE RECORD IS BOSS'S, NOT THE JUDGE'S. Every API call a journey makes is kept as a command of
the acceptance session's record (services/acceptance/session.Command, kind `http`): the call,
the HTTP status and the answer, rendered once as canonical JSON. The judge answers each
acceptance item the way the Acceptance Agent answers one — `AC-2: MET — evidence: E4 (exit 0,
HTTP 200) `"status": "completed"`` — and BOSS's verdict module (acceptance/verdict.judge)
checks every cited call against that record: the call ran, its status is the one claimed, the
quoted words are in its answer. A MET that does not verify is not judged, never met; the journey
is green only when the verdict is `met`.

The default judge is SCRIPTED: it reads each item's expectation (definitions.Item) off the
record, so a daily run spends nothing on judging. A model-written answer can be passed as
`judge=` instead; the verdict module holds it to the same record.

Clients:
  HttpClient   an install's API over HTTP (a service-account key as a bearer token, held in
               memory, never printed or recorded);
  any object with `call(method, path, body) -> (status, body)` and, optionally, `drain()` — the
               release gate's in-process client runs the queued work there (tests/e2e/gate/
               journeys.py).
"""
from __future__ import annotations

import json
import secrets as _secrets
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

from ..acceptance import verdict as _verdict
from ..acceptance.session import Command, CommandRecord
from . import definitions as d

GREEN, RED, PENDING, NOT_RUN = "green", "red", "pending", "not_run"
#: The longest quote a judge's line carries (the verdict module clips an answer at 600).
MAX_QUOTE = 280


class Client(Protocol):
    mode: str

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]: ...


class HttpClient:
    """An install's API. `token`: a service-account API key (Authorization: Bearer)."""

    def __init__(self, base_url: str, token: Optional[str] = None, *, mode: str = "real",
                 timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self._token = token
        self.mode = mode
        self.timeout = timeout

    def __repr__(self) -> str:      # never a credential in a traceback
        return f"HttpClient({self.base!r}, token={'set' if self._token else 'none'})"

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                return resp.status, _decode(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read())

    def drain(self) -> str:
        return "the install's worker runs the queued work"


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        return text


def canonical(status: int, body: Any) -> str:
    """The recorded answer: the status line, then the body as canonical JSON (sorted keys), so a
    nested object's rendering is a substring of the whole and a quote of it can be verified."""
    if body is None:
        rendered = ""
    elif isinstance(body, str):
        rendered = body
    else:
        rendered = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return f"HTTP {status}\n{rendered}".rstrip()


@dataclass
class Run:
    """One journey's run: the record, what each step answered, and where it stopped."""
    journey: d.Journey
    mode: str
    record: CommandRecord = field(default_factory=CommandRecord)
    answers: dict[str, tuple[Command, Any]] = field(default_factory=dict)
    env: dict[str, Any] = field(default_factory=dict)
    stopped: Optional[str] = None


def _redacted(value: Any, held: Mapping[str, str]) -> Any:
    """The body as recorded: the held values (`{{secret.*}}`) never reach the record."""
    if not held:
        return value
    text = json.dumps(value, default=str)
    for v in held.values():
        if v:
            text = text.replace(v, "[held]")
    return json.loads(text)


def _record_call(run: Run, step: d.Step, path: str, body_template: Any, status: Optional[int],
                 answer: Any, started: float, *, attempts: int = 1,
                 error: Optional[str] = None, timed_out: bool = False) -> Command:
    cmd = Command(
        id=run.record.next_id(), kind="http", method=step.method, url=path,
        cmd=f"{step.method} {path}" + (f" (poll {attempts})" if attempts > 1 else ""),
        exit=None if error else 0, status=status, timed_out=timed_out,
        stdout=canonical(status, answer) if status is not None else "",
        stderr=error or "", plain=True, tree_dirty=False,
        body=json.dumps(body_template, default=str) if body_template is not None else None,
        duration_ms=int((time.monotonic() - started) * 1000), started_at=time.time())
    run.record.add(cmd)
    run.answers[step.id] = (cmd, answer)
    return cmd


def _call(client: Client, run: Run, step: d.Step, held: Mapping[str, str],
          sleep: Callable[[float], None]) -> Optional[str]:
    """One call step (polled when it has `until`); returns why the journey must stop, or None."""
    env = {**run.env, **{f"secret.{k}": v for k, v in held.items()}}
    path = d.render(step.path, env)
    body = d.render(step.body, env) if step.body is not None else None
    shown_path = str(_redacted(path, held))
    template = _redacted(body, held)
    started = time.monotonic()
    deadline = started + max(0.0, step.timeout_s)
    attempts = 0
    while True:
        attempts += 1
        try:
            status, answer = client.call(step.method, path, body)
        except Exception as exc:  # noqa: BLE001 — a call that cannot reach BOSS is the record's
            _record_call(run, step, shown_path, template, None, None, started,
                         attempts=attempts, error=f"{type(exc).__name__}: {exc}"[:400])
            return f"{step.id}: {step.call} did not answer ({type(exc).__name__})"
        if step.until is None:
            break
        kind = next(k for k in d.EXPECTS if k in step.until)
        try:
            done = d.holds(kind, step.until[kind], d.select(answer, step.until["path"])[0])
        except d.NoValue:
            done = False
        if done or status >= 400:
            break
        if time.monotonic() >= deadline:
            _record_call(run, step, shown_path, template, status, answer, started,
                         attempts=attempts, timed_out=True)
            return f"{step.id}: still waiting after {int(step.timeout_s)}s"
        if hasattr(client, "drain"):
            client.drain()
        sleep(step.every_s)
    _record_call(run, step, shown_path, template, status, answer, started, attempts=attempts)
    if step.expect_status and status not in step.expect_status:
        return f"{step.id}: {step.call} answered HTTP {status}, expected {list(step.expect_status)}"
    for name, where in step.save.items():
        # `a|b`: the first of these paths the answer has (an import answers `imported` the
        # first time and `already_in_boss` after).
        for option in where.split("|"):
            try:
                run.env[name] = d.select(answer, option.strip())[0]
                break
            except d.NoValue:
                continue
        else:
            return f"{step.id}: the answer has no {where!r} to keep as {name}"
    return None


def _action(client: Client, run: Run, step: d.Step, fakes: Mapping[str, Any]) -> Optional[str]:
    started = time.monotonic()
    if step.do == "drain":
        said = client.drain() if hasattr(client, "drain") else "nothing to drain"
        cmd = Command(id=run.record.next_id(), kind="exec", cmd="drain the queued work",
                      exit=0, stdout=str(said or ""), plain=True, tree_dirty=False,
                      duration_ms=int((time.monotonic() - started) * 1000))
        run.record.add(cmd)
        run.answers[step.id] = (cmd, said)
        return None
    if step.do == "fake_log":
        fake = fakes.get(step.fake or "")
        if fake is None:
            return f"{step.id}: the fake {step.fake} is not running"
        log = fake.log()
        cmd = Command(id=run.record.next_id(), kind="http", method="GET",
                      cmd=f"read what the {step.fake} fake was sent", url=f"fake:{step.fake}",
                      exit=0, status=200, stdout=canonical(200, log), plain=True,
                      tree_dirty=False, duration_ms=int((time.monotonic() - started) * 1000))
        run.record.add(cmd)
        run.answers[step.id] = (cmd, log)
        return None
    return f"{step.id}: unknown action {step.do}"


# ── The judge ────────────────────────────────────────────────────────────────────────────────

def _quote(text: str) -> Optional[str]:
    text = " ".join(str(text).split())[:MAX_QUOTE]
    if len(text) < _verdict.MIN_EXCERPT:
        return None
    if "`" not in text:
        return f"`{text}`"
    if "“" not in text and "”" not in text:
        return f"“{text}”"
    return None


def _excerpt(item: d.Item, cmd: Command, answer: Any) -> tuple[bool, str]:
    """(holds, the words of the record that show it — or why it does not hold)."""
    if item.status is not None and cmd.status != item.status:
        return False, f"{cmd.id} answered HTTP {cmd.status}, not {item.status}"
    if not item.path:
        return True, f"HTTP {cmd.status}"
    try:
        got, shown = d.select(answer, item.path)
    except d.NoValue as exc:
        return False, f"{cmd.id}: {exc}"
    if not d.holds(item.expect, item.value, got):
        return False, (f"{cmd.id}: {item.path} is {json.dumps(got, default=str)[:120]}, "
                       f"expected {item.expect} {json.dumps(item.value, default=str)[:80]}")
    if shown is not None:
        words = json.dumps(shown, sort_keys=True, ensure_ascii=False, default=str)
    else:
        leaf = item.path.rsplit(".", 1)[-1]
        words = f'"{leaf}": ' + json.dumps(got, sort_keys=True, ensure_ascii=False, default=str)
    return True, words


def scripted_judge(run: Run) -> str:
    """The Acceptance Agent's answer, written from the record: MET with the call and the quoted
    words that show each item, MISSING with why. BOSS's verdict module then verifies it."""
    lines = []
    for item in run.journey.acceptance:
        got = run.answers.get(item.step)
        if got is None:
            lines.append(f"AC-{item.n}: MISSING — step {item.step} never ran"
                         + (f" ({run.stopped})" if run.stopped else ""))
            continue
        cmd, answer = got
        ok, words = _excerpt(item, cmd, answer)
        quote = _quote(words) if ok else None
        if ok and quote:
            state = f"exit 0, HTTP {cmd.status}" if cmd.status is not None else "exit 0"
            lines.append(f"AC-{item.n}: MET — evidence: {cmd.id} ({state}) {quote}")
        else:
            lines.append(f"AC-{item.n}: MISSING — {words if not ok else 'nothing quotable'}")
    met = all(" MET — evidence" in line for line in lines)
    lines.append(f"ACCEPTANCE: {'MET' if met else 'NOT MET'}")
    return "\n".join(lines)


# ── One journey ──────────────────────────────────────────────────────────────────────────────

def run_journey(journey: d.Journey, client: Client, *, variables: Optional[Mapping[str, Any]] = None,
                fakes: Optional[Mapping[str, Any]] = None,
                judge: Callable[[Run], str] = scripted_judge,
                sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Play the journey, judge it, and return its result row (results.record stores it):
    {journey_id, title, mode, status (green | red | pending | not_run), why, verdict, items,
     calls, seconds}."""
    mode = getattr(client, "mode", "real")
    started = time.monotonic()
    base = {"journey_id": journey.id, "title": journey.title, "mode": mode, "ticket": journey.ticket}
    if journey.is_pending:
        return {**base, "status": PENDING, "seconds": 0.0, "verdict": None, "items": [],
                "calls": [], "why": f"waits for {journey.pending['waits_for']}: "
                                    f"{journey.pending['why']}"}
    if mode not in journey.modes:
        return {**base, "status": NOT_RUN, "seconds": 0.0, "verdict": None, "items": [],
                "calls": [], "why": f"not runnable on the {mode} engine (runs on "
                                    f"{', '.join(journey.modes)})"}
    fakes = dict(fakes or {})
    held: dict[str, str] = {}
    env: dict[str, Any] = {"stamp": f"{int(time.time())}{_secrets.token_hex(2)}"}
    for name, value in dict(journey.needs.get("defaults") or {}).items():
        try:
            env[name] = d.render(value, env)       # a default may use the run's stamp
        except d.MissingVariable:
            env[name] = value
    env.update(dict(variables or {}))
    for name, fake in fakes.items():
        env[f"fake.{name}.url"] = fake.url
        for k, v in (getattr(fake, "held", {}) or {}).items():
            held[f"{name}.{k}"] = v
    # What the runner must give: every variable a step uses in this mode that no earlier step
    # of this mode keeps (a ticket's title is read from the ticket on a real install, given by
    # the gate on the scripted engine).
    missing = [v for v in journey.variables_for(mode) if v not in env]
    run = Run(journey=journey, mode=mode, env=env)
    if missing:
        run.stopped = f"the runner was not given {', '.join(missing)}"
    else:
        for step in journey.steps:
            if step.only and step.only != mode:
                continue
            try:
                why = (_call(client, run, step, held, sleep) if step.call
                       else _action(client, run, step, fakes))
            except d.MissingVariable as exc:
                why = f"{step.id}: no value for {{{{{exc.args[0]}}}}}"
            if why:
                run.stopped = why
                break
    reply = judge(run)
    with tempfile.TemporaryDirectory(prefix="boss-journey-") as tree:
        record = _verdict.judge([i.as_contract() for i in journey.acceptance], reply, {},
                                root=tree, exec_log=run.record)
    green = record.get("verdict") == "met"
    why = "" if green else (run.stopped or record.get("why") or "; ".join(
        f"AC-{i['n']} {i['status']}: {i.get('evidence') or ''}"[:200]
        for i in record.get("items") or [] if i.get("status") != _verdict.MET))
    return {**base, "status": GREEN if green else RED, "why": why[:1000],
            "verdict": record.get("verdict"), "seconds": round(time.monotonic() - started, 1),
            "items": [{"n": i["n"], "text": i["text"], "status": i["status"],
                       "evidence": str(i.get("evidence") or "")[:400]}
                      for i in record.get("items") or []],
            "calls": run.record.to_list(), "reply": reply}


def run_all(journeys: list[d.Journey], client: Client, *,
            variables: Optional[Mapping[str, Any]] = None,
            fake_factory: Optional[Callable[[str], Any]] = None,
            only: Optional[set[str]] = None,
            sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """Every journey (or `only` these ids), each with the fakes it needs started for it and
    stopped after. A journey that raises is a red row saying so, never a stopped run."""
    out = []
    for j in journeys:
        if only and j.id not in only:
            continue
        fakes: dict[str, Any] = {}
        try:
            if j.runnable_in(getattr(client, "mode", "real")) and fake_factory:
                for name in j.fakes:
                    fakes[name] = fake_factory(name)
            out.append(run_journey(j, client, variables=variables, fakes=fakes, sleep=sleep))
        except Exception as exc:  # noqa: BLE001 — one broken journey must not hide the rest
            out.append({"journey_id": j.id, "title": j.title, "ticket": j.ticket,
                        "mode": getattr(client, "mode", "real"), "status": RED,
                        "why": f"the runner failed: {type(exc).__name__}: {exc}"[:1000],
                        "verdict": None, "items": [], "calls": [], "seconds": 0.0})
        finally:
            for fake in fakes.values():
                try:
                    fake.stop()
                except Exception:  # noqa: BLE001
                    pass
    return out
