"""KG P6a: the external_agent step — a bank's own HTTP-hosted agent runs under BOSS's contract.

BOSS mints a per-execution KG token scoped to exactly the step's contract, calls the agent's
registered endpoint synchronously under the step's timeout, validates its typed JSON output, and
revokes the token the moment the call settles — a late write after that point is simply never
read (the trace-honesty property the PRD names). The postcondition itself (a required write that
never landed) is left to the generic path every other contract step already goes through
(``workflows.complete_step``); this module only has to get the call there and back safely.
"""
from __future__ import annotations

import logging

import time
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.engine import Connection

from . import agent_turns
from .knowledge import context_graph, external_agent_tokens, external_agents, scopes
from .knowledge.step_contracts import parse_contract, policy_from_contract
from .secret_backends import SecretUnavailable, resolve_secret_ref

#: A response body larger than this is refused while it is read, never buffered whole first.
log = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 256 * 1024

DEFAULT_TIMEOUT_SECONDS = 60


def run_step(connection: Connection, ctx: Any, *, project_id: str, step: dict[str, Any],
             context: dict[str, Any]) -> dict[str, Any]:
    from .workflow_capability_runner import CapabilityExecutionError, CapabilityWaitError

    step_id = str(step.get("id") or "?")
    run_id = str(context.get("run_id") or "?")
    captain = step.get("captain") or {}
    agent_id = str(captain.get("external_agent") or "").strip() if isinstance(captain, dict) else ""
    if not agent_id or not external_agents.active_for_project(connection, project_id, agent_id):
        raise CapabilityExecutionError(external_agents.plain_reason("unregistered_agent"))
    agent = external_agents.get(connection, project_id, agent_id)
    if not agent:
        raise CapabilityExecutionError(external_agents.plain_reason("unregistered_agent"))
    # Refuse cheaply, before any credential is minted, if the endpoint resolves somewhere it
    # should not reach — checked on every call, never only at registration. The real call below
    # is pinned to this exact validated address, not a fresh DNS lookup, to close the
    # rebinding TOCTOU where a second resolution at connect time answers differently.
    pinned_ip = external_agents.resolve_validated_address(agent["endpoint_url"])

    contract = parse_contract(step_id, step.get("contract"))
    # max_nodes defaults to 0 on StagePolicy, which the read path treats as literally zero
    # rows — give the external agent the same read bound as every other seeded stage
    # (scopes.DEFAULTS["investigate"]) so a contract's declared reads are actually reachable
    # without a contract author having to set `policy.max_nodes`/`list_cap` explicitly.
    base_policy = scopes.StagePolicy("custom", frozenset({"code", "lifecycle", "knowledge", "ops", "rules"}),
                                      code="seeded", max_hops=2, max_nodes=200)
    policy = policy_from_contract(base_policy, contract) if contract is not None else base_policy
    timeout_seconds = int(step.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)

    mcp_url = getattr(getattr(ctx, "settings", None), "kg_mcp_url", None)
    if not mcp_url:
        raise CapabilityExecutionError(external_agents.plain_reason("kg_mcp_not_configured"))

    settings = getattr(ctx, "settings", None)
    try:
        secret_value = resolve_secret_ref(connection, agent["secret_ref"], settings, project_id=project_id)
    except SecretUnavailable:
        raise CapabilityExecutionError(external_agents.plain_reason("secret_unavailable")) from None

    _phase = _phase_logger(project_id, run_id, step_id, agent_id)
    _phase("minting a scoped knowledge-graph key")
    # COMMITTED BEFORE THE CALL. The step runs inside the runner's open transaction, and the
    # agent spends this token at the knowledge-graph MCP server, another process: minted in the
    # step's transaction it is invisible there, so every read and write-back was refused.
    engine = getattr(ctx, "engine", None)
    if engine is not None:
        with engine.begin() as own:
            issued = external_agent_tokens.mint(
                own, project_id=project_id, run_id=run_id, step_id=step_id,
                external_agent_id=agent_id, policy=policy, ttl_seconds=timeout_seconds)
    else:
        issued = external_agent_tokens.mint(
            connection, project_id=project_id, run_id=run_id, step_id=step_id,
            external_agent_id=agent_id, policy=policy, ttl_seconds=timeout_seconds)

    auth_header = f"Bearer {secret_value}"
    payload = {
        "run_id": run_id, "step_id": step_id, "input": context.get("input") or {},
        "contract": step.get("contract"),
        "kg": {
            "mcp_url": mcp_url,
            "token": issued.token,
            "expires_at": issued.expires_at.isoformat() if hasattr(issued.expires_at, "isoformat")
            else str(issued.expires_at),
        },
    }

    agent_host = urlparse(agent["endpoint_url"]).hostname or agent["endpoint_url"]
    started_at = time.monotonic()
    call_ok = False
    error_text = None
    _phase(f"calling the agent at {urlparse(agent['endpoint_url']).hostname}")
    try:
        output = _call_agent(agent["endpoint_url"], payload, auth_header, timeout_seconds, pinned_ip)
        call_ok = True
        _phase("the agent answered")
    except CapabilityWaitError as error:
        scrubbed = external_agents.scrub(str(error), issued.token, auth_header)
        error_text = scrubbed
        raise CapabilityWaitError(scrubbed, wait_seconds=error.wait_seconds, kind=error.kind) from None
    except CapabilityExecutionError as error:
        scrubbed = external_agents.scrub(str(error), issued.token, auth_header)
        error_text = scrubbed
        raise type(error)(scrubbed) from None
    finally:
        latency_ms = int((time.monotonic() - started_at) * 1000)
        agent_turns.record_turns(
            connection,
            [
                {"turn_index": 0, "direction": "tool_call", "tool_name": agent_host,
                 "content": {"run_id": run_id, "step_id": step_id}, "ok": True, "latency_ms": 0},
                {"turn_index": 1, "direction": "tool_result", "tool_name": agent_host,
                 "content": "ok" if call_ok else (error_text or "failed"), "ok": call_ok,
                 "latency_ms": latency_ms},
            ],
            project_id=project_id, code_task_run_id=run_id, stage=step_id)
        if engine is not None:
            with engine.begin() as own:
                external_agent_tokens.revoke(own, issued.token_id)
        else:
            external_agent_tokens.revoke(connection, issued.token_id)

    # The trace-honesty property: what the step produced is read back from the Context Graph,
    # never trusted from the agent's self-reported HTTP body.
    produced = (context_graph.read_contract_writes(
        connection, project_id, run_id=run_id, step_id=step_id, contract=contract)
        if contract is not None else {})

    # WHAT IT SAID AND WHO IT IS, in words, for the run page (it read "Ext e9286d…" and showed
    # nothing of the review, 2026-11-01): the statement it wrote, read back from the graph.
    said = next((str(v.get("statement")) for v in (produced or {}).values()
                 if isinstance(v, dict) and v.get("statement")), "")
    agent_row = external_agents.get(connection, project_id, agent_id) or {}
    return {
        "kind": "external_agent",
        "produced": produced,
        **({"note": said} if said else {}),
        "ran_as": {"agent": agent_id, "how": "external_agent",
                   "why": "named on the step's captain",
                   **({"name": agent_row["display_name"]} if agent_row.get("display_name") else {})},
    }


def _phase_logger(project_id: str, run_id: str, step_id: str, agent_id: str):
    """Each phase of an outside agent's step, timed in the log: where a step that hangs stopped
    (a live bank-review step timed out with no trace of which phase it was in, 2026-10-31)."""
    started = time.monotonic()

    def phase(what: str) -> None:
        log.info("external agent step %s/%s (%s): %s after %.1fs", run_id, step_id, agent_id,
                 what, time.monotonic() - started)
    return phase


def _call_agent(endpoint_url: str, payload: dict[str, Any], auth_header: str,
                 timeout_seconds: int, pinned_ip: str | None = None) -> dict[str, Any]:
    from .workflow_capability_runner import CapabilityExecutionError, CapabilityWaitError

    headers = {"Authorization": auth_header, "Content-Type": "application/json"}
    request_url = endpoint_url
    extensions: dict[str, Any] = {}
    if pinned_ip is not None:
        parsed = urlparse(endpoint_url)
        host_literal = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_url = parsed._replace(netloc=f"{host_literal}:{port}").geturl()
        headers["Host"] = parsed.hostname or ""
        extensions["sni_hostname"] = parsed.hostname or ""
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            with client.stream("POST", request_url, json=payload, headers=headers,
                                extensions=extensions) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise CapabilityExecutionError(external_agents.plain_reason("oversized_body"))
                status_code = response.status_code
    except httpx.TimeoutException:
        raise CapabilityWaitError(external_agents.plain_reason("timeout"), wait_seconds=30,
                                   kind="external_agent_timeout")
    except httpx.HTTPError as error:
        raise CapabilityExecutionError(external_agents.plain_reason("unreachable")) from None

    if not (200 <= status_code < 300):
        raise CapabilityExecutionError(external_agents.plain_reason("non_2xx"))

    import json

    try:
        parsed = json.loads(bytes(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CapabilityExecutionError(external_agents.plain_reason("invalid_json")) from None

    if not isinstance(parsed, dict):
        raise CapabilityExecutionError(external_agents.plain_reason("invalid_json"))

    return parsed
