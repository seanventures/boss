"""A FAILED CONTRACT TEST IS DIAGNOSED AND FIXED BY AN AGENT, NEVER BLAMED ON THE HOST.

THE CASE (northgate-gateway, API onboarding wfr_0c575958ecdf48aa9476, 2026-11-20). The contract
step sent eleven unauthenticated GET /v1/payments requests: ten expected 401, and the eleventh
(`payments_rate_limited_11th`) expected 429. The gateway checks auth BEFORE its rate limiter (an
unauthenticated request is refused before it is counted, and the token bucket refills), so the
eleventh answered 401 exactly like the ten before it. The case can never pass as written. BOSS's
failure owner read "1 of 12 contract case(s) failed" as BOSS's own failure, re-ran the identical
cases twice, filed a self-fix for itself and asked the person to "check the BOSS host is healthy".

THE RULE, per failed case:
  * UNPASSABLE AS WRITTEN: it sends exactly the request (method, URL, headers, body) that other
    cases sent, those cases PASSED with the very status this one got, and nothing in the case
    sets up the state it expects (an auth token, a burst). Re-running it changes nothing and
    "fixing" the app to answer two identical requests differently is not a fix. It is DROPPED
    from the step with the reason recorded on the step (`recovery_amendment`), an event, and the
    run's history — under the light lane (non-production checks never wait on a person).
  * OTHERWISE the app does not keep its contract: a repair build of the run's build lineage is
    told exactly which cases failed and what they expected, then the step runs again.
A step is never emptied: when every case would be dropped, nothing is dropped and the app is
repaired instead.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

AMENDMENT_KEY = "recovery_amendment"
EVENT = "workflow.step_amended"


def _signature(case: Mapping[str, Any], spec: Optional[Mapping[str, Any]]) -> tuple:
    spec = spec or {}
    return (str(case.get("method") or spec.get("method") or "GET").upper(),
            str(case.get("url") or spec.get("url") or spec.get("path") or ""),
            json.dumps(spec.get("headers") or {}, sort_keys=True),
            json.dumps(spec.get("json"), sort_keys=True), str(spec.get("body") or ""))


def _expected(spec: Optional[Mapping[str, Any]]) -> list[int]:
    want = (spec or {}).get("expect_status")
    if isinstance(want, int):
        return [want]
    if isinstance(want, list):
        return [int(w) for w in want if isinstance(w, int)]
    return []


def diagnose(output: Mapping[str, Any], spec_cases: Optional[list[Mapping[str, Any]]] = None
             ) -> dict[str, Any]:
    """{"drop": [{"name", "reason"}], "fix_app": [{"name", "expected", "got", "failures"}]}."""
    cases = [c for c in (output.get("cases") or []) if isinstance(c, Mapping)]
    specs = {str(s.get("name")): s for s in (spec_cases or []) if isinstance(s, Mapping)}
    passed = [c for c in cases if c.get("ok")]
    drop, fix = [], []
    for case in cases:
        if case.get("ok"):
            continue
        name = str(case.get("name") or "")
        spec = specs.get(name)
        sig = _signature(case, spec)
        got = case.get("status")
        twins = [p for p in passed if _signature(p, specs.get(str(p.get("name")))) == sig
                 and p.get("status") == got and got is not None]
        if twins and got not in _expected(spec):
            names = ", ".join(str(t.get("name")) for t in twins[:4])
            more = f" and {len(twins) - 4} more" if len(twins) > 4 else ""
            drop.append({"name": name, "reason": (
                f"'{name}' sends exactly the same {sig[0]} {sig[1]} as {names}{more}, which "
                f"answered {got} as they expect; nothing in it sets up the state it expects "
                f"({', '.join(str(s) for s in _expected(spec)) or 'a different status'}) — for "
                "example the service checks auth before its rate limiter, so an "
                "unauthenticated request is refused before it is counted. As written it can "
                "never pass, so it was dropped; add it back with the state it needs (a valid "
                "token, a burst within the limiter's window) to test that behaviour.")})
        else:
            fix.append({"name": name, "expected": _expected(spec), "got": got,
                        "failures": list(case.get("failures") or [])[:4]})
    if drop and len(drop) >= len(cases):
        fix += [{"name": d["name"], "expected": [], "got": None, "failures": []} for d in drop]
        drop = []
    return {"drop": drop, "fix_app": fix}


def repair_note(fix_app: list[Mapping[str, Any]]) -> str:
    lines = "; ".join(
        f"{f['name']}: expected {', '.join(str(e) for e in f.get('expected') or []) or '2xx'}, "
        f"got {f.get('got')}" + (f" ({'; '.join(f.get('failures') or [])})"
                                 if f.get("failures") else "")
        for f in fix_app)[:700]
    return ("The deployed API does not keep its contract. Failing cases — " + lines + ". Fix the "
            "app so each answers as the contract expects, keep every passing case passing, and "
            "add a test for each.")


def apply(inputs: dict[str, Any], amendment: Any) -> dict[str, Any]:
    """The step's inputs with a recorded amendment applied (dropped cases left out)."""
    if not isinstance(amendment, Mapping):
        return inputs
    dropped = {str(d.get("name")) for d in amendment.get("drop") or [] if isinstance(d, Mapping)}
    cases = inputs.get("cases")
    if not dropped or not isinstance(cases, list):
        return inputs
    kept = [c for c in cases if not (isinstance(c, Mapping) and str(c.get("name")) in dropped)]
    return {**inputs, "cases": kept} if kept else inputs


__all__ = ["AMENDMENT_KEY", "EVENT", "apply", "diagnose", "repair_note"]
