"""PROBES (AA-E, BOSSAPP-1219; boss-acceptance-agent-lld.md §4.4): the harness-efficiency lever.

A probe is the smallest replayable proof of one item: the command a check ran and what it showed.
Round 1 the Acceptance Agent runs commands and BOSS records every one (session.CommandRecord);
the commands that PROVED an item (a MET whose command evidence verified) become that item's
probes. Round N replays the probes FIRST, with no model: an item whose probe passes again — and
whose files did not change since — is carried as MET (evidence kind 'replay'), and the model is
spent only on what changed, failed, or has no probe.

  {"item": "AC-3" | "F-12", "kind": "exec" | "http",
   "setup": ["make build"],                       # run first, must exit 0
   "action": {"cmd": "curl -s -o /dev/null -w %{http_code} http://127.0.0.1:8081/payments"}
           | {"method": "GET", "url": "http://127.0.0.1:8081/payments", "headers": {}, "body": None},
   "expect": {"exit": 0, "stdout_contains": "401"} | {"status": 401, "stdout_contains": "..."},
   "files": ["internal/auth/jwt.go"],              # the item's files: changed -> not carried
   "source": {"check_id": "acc_...", "command_id": "E4"}}
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

PASS, FAIL, ERROR = "pass", "fail", "error"


def _field(cmd: Any, name: str, default: Any = None) -> Any:
    if cmd is None:
        return default
    if isinstance(cmd, Mapping):
        return cmd.get(name, default)
    return getattr(cmd, name, default)


def _ws(text: Any) -> str:
    return " ".join(str(text or "").split())


def matches(cmd: Any, expect: Mapping[str, Any]) -> Optional[bool]:
    """Whether a recorded command showed what a probe expects; None when it could not run (an
    infrastructure problem — refused, timed out, no exit — never a fail of the work)."""
    if cmd is None or _field(cmd, "refused") or _field(cmd, "timed_out") \
            or _field(cmd, "exit") is None:
        return None
    if "exit" in expect and int(_field(cmd, "exit")) != int(expect["exit"]):
        return False
    if "exit_not" in expect and int(_field(cmd, "exit")) == int(expect["exit_not"]):
        return False
    if "status" in expect and _field(cmd, "status") != int(expect["status"]):
        return False
    if expect.get("status_any_of") and _field(cmd, "status") not in [
            int(x) for x in expect["status_any_of"]]:
        return False
    want = _ws(expect.get("stdout_contains"))
    if want and want not in _ws(f"{_field(cmd, 'stdout', '')}\n{_field(cmd, 'stderr', '')}"):
        return False
    return True


def from_command(item: str, cmd: Any, *, excerpt: Optional[str] = None,
                 files: Iterable[str] = (), check_id: Optional[str] = None) -> dict[str, Any]:
    """The probe that replays one recorded command: the same command, expecting the exit (and
    status) it had and the words the evidence quoted."""
    if _field(cmd, "kind") == "http":
        action = {k: _field(cmd, k) for k in ("method", "url", "headers", "body")}
        expect: dict[str, Any] = {"status": _field(cmd, "status")}
        kind = "http"
    else:
        action = {"cmd": _field(cmd, "cmd")}
        expect = {"exit": _field(cmd, "exit")}
        kind = "exec"
    if excerpt:
        expect["stdout_contains"] = _ws(excerpt)
    return {"item": str(item), "kind": kind, "setup": list(_field(cmd, "setup") or []),
            "action": action, "expect": expect, "files": sorted(set(files or ())),
            "source": {"check_id": check_id, "command_id": _field(cmd, "id")}}


def from_record(record: Mapping[str, Any], exec_log: Any, *,
                check_id: Optional[str] = None) -> list[dict[str, Any]]:
    """The probes a judged check leaves behind: for each item MET on verified command evidence
    AND at least one verified tree file, one probe per verified command (its files: those tree
    files). An item proven by a command alone leaves no probe: with no file to watch, a later
    change could never send it back to the agent. Items carried by replay keep their probe."""
    out: list[dict[str, Any]] = []
    getter = getattr(exec_log, "get", None)
    for item in record.get("items") or []:
        if item.get("status") != "met":
            continue
        key = str(item.get("id") or item.get("n"))
        refs = item.get("evidence_refs") or []
        files = [r["ref"] for r in refs if r.get("kind") == "file" and r.get("verified")]
        if not files:
            continue
        for r in refs:
            if r.get("kind") == "exec" and r.get("verified") and callable(getter):
                cmd = getter(r["ref"])
                if cmd is not None:
                    out.append(from_command(key, cmd, excerpt=r.get("excerpt"), files=files,
                                            check_id=check_id))
    for p in record.get("carried_probes") or []:
        out.append(dict(p))
    return out


def changed(probe: Mapping[str, Any], changed_files: Optional[Iterable[str]]) -> bool:
    """Whether the probe's item may have changed: one of its files is in the diff — or the diff
    is unknown (None), or the probe names no file. Fails closed: only a known, clean diff over
    named files lets a passing probe carry its item."""
    files = set(probe.get("files") or [])
    if changed_files is None or not files:
        return True
    return bool(files & set(changed_files))


def replay(sbx: Any, probes: Iterable[Mapping[str, Any]], *,
           changed_files: Optional[Iterable[str]] = None,
           timeout: int = 120) -> dict[str, dict[str, Any]]:
    """Run every probe again through the sandbox (each command recorded), NO MODEL. Per item:
    {"result": pass | fail | error, "command_ids": [...], "probe": the probe, "changed": bool,
     "check_id"}. An item with several probes passes only when every one passes; any infra
    error makes it `error` (re-checked by the agent, never carried)."""
    out: dict[str, dict[str, Any]] = {}
    for probe in probes or []:
        item = str(probe.get("item") or "")
        if not item:
            continue
        ids: list[str] = []
        result = PASS
        for step in probe.get("setup") or []:
            cmd = sbx.exec(str(step), timeout=timeout, replay=True)
            ids.append(cmd.id)
            if matches(cmd, {"exit": 0}) is not True:
                result = ERROR
                break
        if result == PASS:
            action = probe.get("action") or {}
            if probe.get("kind") == "http" or action.get("url"):
                cmd = sbx.http(str(action.get("method") or "GET"), str(action.get("url") or ""),
                               headers=action.get("headers") or None, body=action.get("body"),
                               timeout=timeout, replay=True)
            else:
                cmd = sbx.exec(str(action.get("cmd") or ""), timeout=timeout, replay=True)
            ids.append(cmd.id)
            ok = matches(cmd, probe.get("expect") or {})
            result = ERROR if ok is None else PASS if ok else FAIL
        prior = out.get(item)
        merged = result if prior is None else (
            ERROR if ERROR in (prior["result"], result)
            else FAIL if FAIL in (prior["result"], result) else PASS)
        out[item] = {"result": merged, "command_ids": (prior or {}).get("command_ids", []) + ids,
                     "probe": dict(probe),
                     "changed": bool((prior or {}).get("changed")) or changed(probe,
                                                                              changed_files),
                     "check_id": (probe.get("source") or {}).get("check_id")}
    return out


def carried(replayed: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """The replayed items carried as MET without a model: passed, and their files unchanged."""
    return {k: dict(v) for k, v in (replayed or {}).items()
            if v.get("result") == PASS and not v.get("changed")}


__all__ = ["ERROR", "FAIL", "PASS", "carried", "changed", "from_command", "from_record",
           "matches", "replay"]
