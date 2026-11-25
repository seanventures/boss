"""The golden journeys' definition format: one JSON file per journey under `defs/`.

A journey is the whole job a preview developer does, written as BOSS API calls, with the items
that make it green. The runner (runner.py) plays the steps against an install's API and records
every call; the Acceptance Agent's verdict module (services/acceptance/verdict.py) then judges
each acceptance item against that record. Nothing in a definition is code.

    {
      "id": "J1",                         J1..J9 are the preview plan's journeys; J10+ are added
      "title": "...",  "ticket": "BOSSAPP-1284",  "summary": "...",
      "modes": ["scripted", "real"],      where it can run: the scripted engine (no model; the
                                          release gate's journeys tier) and/or a real install
      "pending": {"waits_for": "PR #329", "why": "..."}     optional: not runnable on main yet
      "needs": {"fakes": ["mcp_http"], "fixtures": ["plain-node"], "vars": ["owner", "repo"],
                "scripted": {"faults": "failing_test"}},
      "steps": [
        {"id": "start", "call": "POST /v1/code-tasks", "body": {...},
         "expect_status": [200], "save": {"run_id": "run_id"}},
        {"id": "work", "do": "drain"},
        {"id": "done", "call": "GET /v1/code-tasks/{{run_id}}",
         "until": {"path": "status", "in": ["completed", "failed"]}, "timeout_s": 2400}
      ],
      "acceptance": [
        {"text": "The build finished as completed", "step": "done", "path": "status",
         "equals": "completed"}
      ]
    }

`{{name}}` in a call, a body or a path is a variable: `stamp` (unique per run), `project_id`,
what an earlier step saved, what the runner was given (`--var owner=...`), and a started fake's
`fake.<name>.url`. `{{secret.<name>}}` is a value the runner holds and never records: the
recorded call keeps the template, not the value.

An acceptance item names a step and what its response must show: `status` (the HTTP status),
and/or a `path` into the JSON with one of `equals`, `in`, `present`, `contains`, `at_least`. A
path is dotted keys, `[n]` for an index and `[key=value]` for the element of a list whose key
has that value (`gates[name=acceptance_met].ok`).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFS_DIR = Path(__file__).resolve().parent / "defs"

MODES = ("scripted", "real")
#: Step actions that are not an API call. `drain`: run the queued work now (the release gate,
#: which has no worker); on an install the worker does it and this is a no-op. `fake_log`: what
#: a started fake was sent, recorded as a call so an acceptance item can cite it.
ACTIONS = ("drain", "fake_log")
EXPECTS = ("equals", "in", "present", "contains", "at_least")
_CALL = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")
_ID = re.compile(r"^J\d{1,3}$")


class DefinitionError(ValueError):
    """A journey file that cannot be run; the message names the file and the field."""


@dataclass(frozen=True)
class Step:
    id: str
    call: Optional[str] = None          # "METHOD /path"
    do: Optional[str] = None            # an ACTIONS entry
    body: Any = None
    expect_status: tuple[int, ...] = ()
    save: dict[str, str] = field(default_factory=dict)
    until: Optional[dict[str, Any]] = None
    timeout_s: float = 0.0
    every_s: float = 10.0
    only: Optional[str] = None          # run in this mode only
    fake: Optional[str] = None          # fake_log: which fake

    @property
    def method(self) -> str:
        return (self.call or "").split(" ", 1)[0]

    @property
    def path(self) -> str:
        return (self.call or " ").split(" ", 1)[1]


@dataclass(frozen=True)
class Item:
    n: int
    text: str
    step: str
    status: Optional[int] = None
    path: Optional[str] = None
    expect: Optional[str] = None        # an EXPECTS entry
    value: Any = None

    def as_contract(self) -> dict[str, Any]:
        """The item as the verdict module reads one (contract items: n, id, text)."""
        return {"n": self.n, "id": f"AC-{self.n}", "text": self.text}


@dataclass(frozen=True)
class Journey:
    id: str
    title: str
    summary: str
    ticket: str
    modes: tuple[str, ...]
    steps: tuple[Step, ...]
    acceptance: tuple[Item, ...]
    pending: Optional[dict[str, str]] = None
    needs: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def is_pending(self) -> bool:
        return bool(self.pending)

    def runnable_in(self, mode: str) -> bool:
        return not self.is_pending and mode in self.modes

    @property
    def fakes(self) -> tuple[str, ...]:
        return tuple(self.needs.get("fakes") or ())

    def variables_for(self, mode: str) -> list[str]:
        """The variables the runner must supply in `mode`: those the steps that run there use,
        less what those steps keep (`save`), the fakes' own and `{{secret.*}}`."""
        used: set[str] = set()
        kept: set[str] = {"stamp"}
        for s in self.steps:
            if s.only and s.only != mode:
                continue
            used |= variables_in(s.call or "") | variables_in(s.body) | variables_in(s.until or {})
            kept |= set(s.save)
        return sorted(v for v in used - kept
                      if not v.startswith(("fake.", "secret.")) and v not in (self.needs.get("defaults") or {}))

    def summary_row(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "ticket": self.ticket,
                "modes": list(self.modes), "pending": dict(self.pending or {}) or None}


def _fail(source: str, what: str) -> DefinitionError:
    return DefinitionError(f"{Path(source).name or source}: {what}")


def parse(raw: dict[str, Any], source: str = "") -> Journey:
    """One journey from its JSON, checked field by field."""
    if not isinstance(raw, dict):
        raise _fail(source, "a journey is a JSON object")
    jid = str(raw.get("id") or "")
    if not _ID.match(jid):
        raise _fail(source, f"id must look like J1, got {jid!r}")
    for key in ("title", "summary", "ticket"):
        if not str(raw.get(key) or "").strip():
            raise _fail(source, f"{key} is required")
    modes = tuple(raw.get("modes") or ())
    if not modes or any(m not in MODES for m in modes):
        raise _fail(source, f"modes must be a non-empty subset of {list(MODES)}, got {list(modes)}")
    pending = raw.get("pending")
    if pending is not None and not (isinstance(pending, dict) and pending.get("waits_for")
                                    and pending.get("why")):
        raise _fail(source, "pending needs waits_for and why")
    steps: list[Step] = []
    seen: set[str] = set()
    for i, s in enumerate(raw.get("steps") or []):
        sid = str(s.get("id") or "")
        if not sid or sid in seen:
            raise _fail(source, f"step {i + 1} needs a unique id")
        seen.add(sid)
        call, do = s.get("call"), s.get("do")
        if bool(call) == bool(do):
            raise _fail(source, f"step {sid}: give exactly one of call or do")
        if call and not _CALL.match(str(call)):
            raise _fail(source, f"step {sid}: call must be 'METHOD /path', got {call!r}")
        if do and do not in ACTIONS:
            raise _fail(source, f"step {sid}: do must be one of {list(ACTIONS)}")
        if do == "fake_log" and s.get("fake") not in (raw.get("needs") or {}).get("fakes", []):
            raise _fail(source, f"step {sid}: fake_log names a fake the journey does not need")
        only = s.get("only")
        if only is not None and only not in MODES:
            raise _fail(source, f"step {sid}: only must be one of {list(MODES)}")
        until = s.get("until")
        if until is not None:
            _check_expect(source, f"step {sid} until", until)
        steps.append(Step(
            id=sid, call=call, do=do, body=s.get("body"),
            expect_status=tuple(int(x) for x in (s.get("expect_status") or ())),
            save={str(k): str(v) for k, v in (s.get("save") or {}).items()},
            until=until, timeout_s=float(s.get("timeout_s") or (600 if until else 0)),
            every_s=float(s.get("every_s") or 10), only=only, fake=s.get("fake")))
    if not steps:
        raise _fail(source, "a journey needs at least one step")
    items: list[Item] = []
    for n, a in enumerate(raw.get("acceptance") or [], start=1):
        text = str(a.get("text") or "").strip()
        if not text:
            raise _fail(source, f"acceptance item {n} needs text")
        if a.get("step") not in seen:
            raise _fail(source, f"acceptance item {n} names step {a.get('step')!r}, not a step here")
        kind = next((k for k in EXPECTS if k in a), None)
        if a.get("path") and kind is None:
            raise _fail(source, f"acceptance item {n}: a path needs one of {list(EXPECTS)}")
        if not a.get("path") and a.get("status") is None:
            raise _fail(source, f"acceptance item {n}: say a status or a path to check")
        items.append(Item(n=n, text=text, step=str(a["step"]),
                          status=int(a["status"]) if a.get("status") is not None else None,
                          path=a.get("path"), expect=kind,
                          value=a.get(kind) if kind else None))
    if not items:
        raise _fail(source, "a journey is green only on its acceptance items: give at least one")
    needs = dict(raw.get("needs") or {})
    return Journey(id=jid, title=str(raw["title"]).strip(), summary=str(raw["summary"]).strip(),
                   ticket=str(raw["ticket"]).strip(), modes=modes, steps=tuple(steps),
                   acceptance=tuple(items), pending=dict(pending) if pending else None,
                   needs=needs, source=source)


def _check_expect(source: str, where: str, spec: dict[str, Any]) -> None:
    if not isinstance(spec, dict) or not spec.get("path") or not any(k in spec for k in EXPECTS):
        raise _fail(source, f"{where}: needs a path and one of {list(EXPECTS)}")


def load(path: Path) -> Journey:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _fail(str(path), f"cannot read it: {exc}") from exc
    return parse(raw, str(path))


def _order(j: Journey) -> int:
    return int(j.id[1:])


def load_all(directory: Optional[Path] = None) -> list[Journey]:
    """Every journey in `directory` (default: the shipped defs), in J-number order; ids unique."""
    found = [load(p) for p in sorted(Path(directory or DEFS_DIR).glob("*.json"))]
    ids = [j.id for j in found]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise DefinitionError(f"journey ids used twice: {dup}")
    return sorted(found, key=_order)


# ── Templates and paths ──────────────────────────────────────────────────────────────────────

_VAR = re.compile(r"\{\{\s*([\w.\-]+)\s*\}\}")


class MissingVariable(KeyError):
    pass


def render(value: Any, env: dict[str, Any]) -> Any:
    """`{{name}}` filled from `env`, recursively. A string that is ONE variable keeps its type."""
    if isinstance(value, str):
        whole = _VAR.fullmatch(value.strip())
        if whole:
            return _lookup_var(whole.group(1), env)
        return _VAR.sub(lambda m: str(_lookup_var(m.group(1), env)), value)
    if isinstance(value, list):
        return [render(v, env) for v in value]
    if isinstance(value, dict):
        return {k: render(v, env) for k, v in value.items()}
    return value


def _lookup_var(name: str, env: dict[str, Any]) -> Any:
    if name in env:
        return env[name]
    raise MissingVariable(name)


def variables_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_VAR.findall(value))
    if isinstance(value, list):
        return set().union(*(variables_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(variables_in(v) for v in value.values())) if value else set()
    return set()


_SEG = re.compile(r"([^.\[\]]+)|\[(\d+)\]|\[([^=\]]+)=([^\]]*)\]")


class NoValue(LookupError):
    pass


def select(data: Any, path: str) -> tuple[Any, Any]:
    """(the value at `path`, the smallest JSON object that shows it: the list element a filter
    picked, or None when the value hangs off plain keys). Raises NoValue."""
    cur, shown = data, None
    for m in _SEG.finditer(path):
        key, idx, fkey, fval = m.groups()
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                raise NoValue(f"no {key!r} at {path}")
            cur = cur[key]
        elif idx is not None:
            if not isinstance(cur, list) or int(idx) >= len(cur):
                raise NoValue(f"no element {idx} at {path}")
            cur, shown = cur[int(idx)], cur[int(idx)]
        else:
            if not isinstance(cur, list):
                raise NoValue(f"not a list where {path} filters")
            hit = next((e for e in cur if isinstance(e, dict) and str(e.get(fkey)) == fval), None)
            if hit is None:
                raise NoValue(f"no element with {fkey}={fval} at {path}")
            cur, shown = hit, hit
    return cur, shown


def holds(kind: Optional[str], want: Any, got: Any) -> bool:
    if kind == "equals":
        return got == want
    if kind == "in":
        return got in (want or [])
    if kind == "present":
        return (got not in (None, "", [], {})) == bool(want)
    if kind == "contains":
        return str(want) in (got if isinstance(got, str) else json.dumps(got, default=str))
    if kind == "at_least":
        try:
            return float(len(got) if isinstance(got, (list, dict)) else got) >= float(want)
        except (TypeError, ValueError):
            return False
    return False
