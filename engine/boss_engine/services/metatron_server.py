"""MT-10 BOSS server services for Metatron: capabilities, signed policy bundles, server-side
reviewer, TTL grants, credential-safe audit ingest, and host credential leases.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import text

from ..config import Settings
from ..identity import authn_support, jwks, lineage, signing_keys
from ..tools import host_diagnostics_rules
from . import deployment_fleet, mcp_registry, redaction, secret_backends, tool_policy_engine
from .agent_decision import Candidate, SpendEnvelope
from .agent_service import AgentService
from .core import emit_event, make_id
from .metatron_turns import MetatronBudgetError, MetatronStepSpend
from .receipts import ReceiptService

MIN_METATRON_CLIENT = (0, 1, 0)
MIN_METATRON_CLIENT_STR = "0.1.0"
MIN_BOSS_CONTRACT = 1
BOSS_CONTRACT_VERSION = 1

POLICY_JWS_TYP = "boss-policy+jws"
HARD_RISK_CLASSES = frozenset({"capture", "secret-use", "app-control"})
RISK_RANK = {
    "read": 0,
    "read-only": 0,
    "local-read": 0,
    "remote-read": 0,
    "write-scratch": 1,
    "write-local": 2,
    "remote-write": 2,
    "capture": 3,
    "secret-use": 4,
    "app-control": 5,
}
FORBIDDEN_AUDIT_KEYS = frozenset(
    {
        "cookie_values",
        "cookie_value",
        "cookies",
        "token",
        "access_token",
        "refresh_token",
        "raw_secret",
        "file_contents",
        "private_key",
        "authorization",
        "password",
        "passwd",
        "secret",
        "api_key",
    }
)


class MetatronServerError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
            }
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return fallback
    return value if value is not None else fallback


def capabilities_document(boss_version: str) -> dict[str, Any]:
    return {
        "contract": BOSS_CONTRACT_VERSION,
        "min_client": MIN_METATRON_CLIENT_STR,
        "boss_version": boss_version,
        "features": [
            "turns.stream",
            "tools.client",
            "policy",
            "audit",
            "device_login",
            "credential_lease",
        ],
        "limits": {
            "tool_result_inline_bytes": 65_536,
            "turn_deadline_s": 900,
        },
    }


def _fallback_policy_key() -> tuple[str, Ed25519PrivateKey]:
    raw_server = authn_support.server_key()
    seed = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"boss-metatron-policy",
        info=b"boss-metatron-policy-jws-v1",
    ).derive(raw_server)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    jwk = jwks.public_jwk(priv.public_key())
    kid = "metatron-policy-" + hashlib.sha256(jwk["x"].encode("ascii")).hexdigest()[:12]
    return kid, priv


def _active_policy_signing_key(connection: Any) -> tuple[str, Ed25519PrivateKey]:
    try:
        key = signing_keys.active_key("actor", db=connection)
        return key.kid, key.private_key
    except Exception:  # noqa: BLE001 — unvaulted dev/test installs use deterministic server_key HKDF
        return _fallback_policy_key()


def policy_public_jwks(connection: Any) -> dict[str, Any]:
    """Read-only JWKS for verifying signed `/v1/metatron/policy` bundles (zero DB writes)."""
    keys: list[dict[str, Any]] = []
    seen_kids: set[str] = set()
    rows = connection.execute(
        text(
            "SELECT public_jwk FROM identity_signing_keys "
            "WHERE family = 'actor' AND (status = 'active' OR (status = 'retiring' AND retire_after > now())) "
            "ORDER BY created_at"
        )
    ).mappings().all()
    for r in rows:
        jwk = dict(_loads(r["public_jwk"], {}))
        jwk.pop("d", None)
        kid = str(jwk.get("kid") or "")
        if jwk and kid not in seen_kids:
            keys.append(jwk)
            seen_kids.add(kid)
    if not keys:
        fb_kid, fb_priv = _fallback_policy_key()
        keys.append(jwks.public_jwk(fb_priv.public_key(), kid=fb_kid))
    return {"keys": keys}


def _effect_to_decision(effect: str) -> str:
    if effect == "allow":
        return "allow"
    if effect == "ask":
        return "prompt"
    return "forbidden"


def _builtin_hard_rules() -> list[dict[str, Any]]:
    return [
        {
            "id": "r-pcap-hard",
            "match": {"server": "metatron", "tool": "pcap", "conditions": []},
            "decision": "prompt",
            "hard": True,
            "justification": "Packet capture requires explicit operator approval.",
            "examples": {
                "match": [{"server": "metatron", "tool": "pcap", "arguments": {"interface": "eth0"}}],
                "not_match": [{"server": "metatron", "tool": "ssh_run", "arguments": {"argv": ["uptime"]}}],
            },
        },
        {
            "id": "r-app-control-hard",
            "match": {"server": "metatron", "tool": "app_control", "conditions": []},
            "decision": "prompt",
            "hard": True,
            "justification": "Desktop automation runs unsandboxed and requires explicit operator approval.",
            "examples": {
                "match": [{"server": "metatron", "tool": "app_control", "arguments": {"app": "Terminal"}}],
                "not_match": [{"server": "metatron", "tool": "shell_local", "arguments": {"argv": ["ls"]}}],
            },
        },
        {
            "id": "r-secret-use-hard",
            "match": {"server": "metatron", "tool": "secret_use", "conditions": []},
            "decision": "prompt",
            "hard": True,
            "justification": "Using raw credentials requires explicit operator approval.",
            "examples": {
                "match": [{"server": "metatron", "tool": "secret_use", "arguments": {}}],
                "not_match": [{"server": "metatron", "tool": "view_image", "arguments": {"path": "x.png"}}],
            },
        },
    ]


def _builtin_hard_rule_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for br in _builtin_hard_rules():
        match = br.get("match") or {}
        rows.append(
            {
                "id": br["id"],
                "scope": "org",
                "priority": 0,
                "effect": "ask" if br.get("decision") == "prompt" else str(br.get("decision") or "ask"),
                "selector": {
                    "server": match.get("server") or "metatron",
                    "tool": match.get("tool") or "*",
                },
                "conditions": list(match.get("conditions") or []),
                "hard": bool(br.get("hard", True)),
                "status": "active",
            }
        )
    return rows


_BUILTIN_HARD_TOOLS = frozenset(
    str((br.get("match") or {}).get("tool") or "").strip()
    for br in _builtin_hard_rules()
    if br.get("hard") and str((br.get("match") or {}).get("tool") or "").strip() not in ("", "*")
)


def build_and_sign_policy_bundle(
    connection: Any, project_id: str
) -> tuple[dict[str, Any], str]:
    """Read-only policy bundle builder + Ed25519 compact JWS signer (zero DB writes)."""
    org_id = tool_policy_engine.org_of(connection, project_id)
    if org_id is None:
        proj = connection.execute(
            text("SELECT id FROM projects WHERE id = :p"), {"p": project_id}
        ).scalar()
        if not proj:
            raise MetatronServerError(404, "project_not_found", f"Project {project_id} was not found.")

    raw_rules = tool_policy_engine.order_rules(
        tool_policy_engine.load_rules(connection, project_id, org_id)
    )
    rules: list[dict[str, Any]] = []
    for r in raw_rules:
        sel = r.get("selector") or {}
        rules.append(
            {
                "id": r["id"],
                "scope": r["scope"],
                "priority": int(r.get("priority") or 100),
                "match": {
                    "server": sel.get("server") or "*",
                    "tool": sel.get("tool") or "*",
                    "conditions": list(r.get("conditions") or []),
                },
                "decision": _effect_to_decision(str(r.get("effect") or "ask")),
                "hard": bool(r.get("hard", False)),
                "justification": str(r.get("note") or ""),
                "examples": r.get("examples") or {"match": [], "not_match": []},
            }
        )
    rules.extend(_builtin_hard_rules())

    mcp_servers = [
        {
            "id": s["id"],
            "name": s["name"],
            "transport": s.get("transport"),
            "url": s.get("url"),
            "trusted": bool(s.get("trusted")),
            "tools": [
                {
                    "name": t.get("name"),
                    "description": t.get("description") or "",
                    "risk_tier": t.get("risk_tier"),
                }
                for t in (s.get("tools") or [])
                if isinstance(t, Mapping) and t.get("name")
            ],
        }
        for s in mcp_registry.list_mcp_servers(connection, project_id, enabled_only=True)
    ]

    host_diagnostics = host_diagnostics_rules.policy_rule_table()
    tools_allowed = [
        "shell_local",
        "ssh_run",
        "pyrun",
        "transfer",
        "pcap",
        "view_image",
        "http_session",
    ]
    core = {
        "project_id": project_id,
        "mode_floor": "ask",
        "tools_allowed": tools_allowed,
        "rules": rules,
        "host_diagnostics": host_diagnostics,
        "cookie_mode": {"enabled": False, "domains": []},
        "mcp": {"servers": mcp_servers},
    }
    version = hashlib.sha256(jwks.canonical(core)).hexdigest()[:16]
    kid, priv = _active_policy_signing_key(connection)
    etag = f'"{version}:{kid}"'

    now = _now()
    bundle = {
        "version": version,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        **core,
    }
    token = jwks.sign({"alg": jwks.ALG, "typ": POLICY_JWS_TYP, "kid": kid}, bundle, priv)
    return (
        {
            "bundle": bundle,
            "jws": token,
            "kid": kid,
            "alg": jwks.ALG,
            "typ": POLICY_JWS_TYP,
        },
        etag,
    )


def etag_matches(if_none_match: Optional[str], current_etag: str) -> bool:
    if not if_none_match:
        return False
    norm_cur = current_etag.strip().removeprefix("W/").strip().strip('"')
    for candidate in str(if_none_match).split(","):
        c = candidate.strip()
        if c == "*":
            return True
        if c.removeprefix("W/").strip().strip('"') == norm_cur:
            return True
    return False


def create_grant(
    connection: Any,
    *,
    org_id: str,
    project_id: str,
    principal_id: str,
    actor: str,
    scope: Optional[Mapping[str, Any]],
    allowed_tools: Optional[list[str]],
    max_risk_class: Optional[str],
    ttl_s: int,
) -> dict[str, Any]:
    if not 60 <= int(ttl_s) <= 86_400:
        raise MetatronServerError(
            422, "invalid_ttl", "ttl_s must be between 60 and 86400 seconds."
        )
    if max_risk_class and max_risk_class not in RISK_RANK:
        raise MetatronServerError(
            422, "invalid_risk_class", f"Unknown max_risk_class {max_risk_class!r}."
        )
    grant_id = make_id("mgr")
    expires_at = _now() + timedelta(seconds=int(ttl_s))
    tools = [str(t).strip() for t in (allowed_tools or []) if str(t).strip()]
    scope_obj = dict(scope or {})
    connection.execute(
        text(
            "INSERT INTO metatron_grants "
            "(id, org_id, project_id, principal_id, scope, allowed_tools, max_risk_class, "
            " ttl_seconds, expires_at) "
            "VALUES (:id, :o, :p, :pr, CAST(:sc AS JSONB), :at, :mr, :ttl, :exp)"
        ),
        {
            "id": grant_id,
            "o": org_id,
            "p": project_id,
            "pr": principal_id,
            "sc": json.dumps(scope_obj),
            "at": tools,
            "mr": max_risk_class,
            "ttl": int(ttl_s),
            "exp": expires_at,
        },
    )
    emit_event(
        connection,
        project_id,
        "metatron.grant_created",
        actor,
        "metatron_grant",
        grant_id,
        {"principal_id": principal_id, "ttl_seconds": int(ttl_s), "allowed_tools": tools},
    )
    return {
        "grant_id": grant_id,
        "project_id": project_id,
        "principal_id": principal_id,
        "scope": scope_obj,
        "allowed_tools": tools,
        "max_risk_class": max_risk_class,
        "ttl_seconds": int(ttl_s),
        "expires_at": expires_at.isoformat(),
    }


def mark_grant_revoked(
    connection: Any,
    *,
    grant_id: str,
    principal_id: str,
) -> bool:
    row = connection.execute(
        text(
            "UPDATE metatron_grants SET revoked_at = now(), revoked_by = :p "
            "WHERE id = :id AND principal_id = :p AND revoked_at IS NULL "
            "RETURNING project_id"
        ),
        {"id": grant_id, "p": principal_id},
    ).scalar()
    if not row:
        return False
    emit_event(
        connection,
        str(row),
        "metatron.grant_revoked",
        principal_id,
        "metatron_grant",
        grant_id,
        {"principal_id": principal_id},
    )
    return True


revoke_grant = mark_grant_revoked


def _grant_covers(
    grant: Mapping[str, Any],
    tool: str,
    risk_class: str,
    call: Optional[Mapping[str, Any]] = None,
) -> bool:
    allowed = list(grant.get("allowed_tools") or [])
    if allowed and tool not in allowed and "*" not in allowed:
        return False
    max_risk = grant.get("max_risk_class")
    if max_risk and RISK_RANK.get(risk_class, 99) > RISK_RANK.get(str(max_risk), -1):
        return False

    scope = _loads(grant.get("scope"), {})
    if scope is None:
        scope = {}
    if not isinstance(scope, Mapping):
        return False
    if not scope:
        return True

    call_map = call if isinstance(call, Mapping) else {}
    call_args = call_map.get("arguments") if isinstance(call_map.get("arguments"), Mapping) else {}
    raw_host = call_map.get("host") if call_map.get("host") is not None else call_args.get("host")
    raw_domain = (
        call_map.get("domain") if call_map.get("domain") is not None else call_args.get("domain")
    )
    call_host = str(raw_host).strip() if isinstance(raw_host, str) else ""
    call_domain = str(raw_domain).strip() if isinstance(raw_domain, str) else ""

    for key, raw_val in scope.items():
        if raw_val is None or raw_val == "" or raw_val == [] or raw_val == {}:
            continue
        if key in ("hosts", "host"):
            if isinstance(raw_val, str):
                allowed_hosts = [raw_val.strip()] if raw_val.strip() else []
            elif isinstance(raw_val, (list, tuple, set)):
                allowed_hosts = [str(v).strip() for v in raw_val if isinstance(v, str) and v.strip()]
            else:
                return False
            if not allowed_hosts or not call_host or call_host not in allowed_hosts:
                return False
        elif key in ("domains", "domain"):
            if isinstance(raw_val, str):
                allowed_domains = [raw_val.strip().lower()] if raw_val.strip() else []
            elif isinstance(raw_val, (list, tuple, set)):
                allowed_domains = [
                    str(v).strip().lower() for v in raw_val if isinstance(v, str) and v.strip()
                ]
            else:
                return False
            if not allowed_domains or not call_domain or call_domain.lower() not in allowed_domains:
                return False
        else:
            # Fail closed on any unrecognized non-empty scope constraint.
            return False
    return True


def review_approval(
    engine: Any,
    settings: Settings,
    *,
    project_id: str,
    principal_id: str,
    org_id: Optional[str],
    actor: str,
    session_id: str,
    turn_id: Optional[str],
    call: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    """Server-side budgeted reviewer for `act-on-my-behalf` calls.

    Never approves `hard:true` rules, `forbidden` policy decisions, or hard risk classes
    (`capture`, `secret-use`, `app-control`). Requires an active covering grant.
    """
    tool = str(call.get("tool") or "").strip()
    if not tool:
        raise MetatronServerError(422, "invalid_call", "call.tool is required.")
    risk_class = str(call.get("risk_class") or "write-local").strip()
    call_args = call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {}
    argv_redacted = (
        call.get("argv_redacted")
        if call.get("argv_redacted") is not None
        else (call_args.get("argv") or [])
    )
    host_val = call.get("host") if call.get("host") is not None else call_args.get("host")
    domain_val = call.get("domain") if call.get("domain") is not None else call_args.get("domain")
    args_ctx = {
        **{k: v for k, v in call_args.items() if k not in ("argv", "host", "domain", "risk_class")},
        "argv": argv_redacted,
        "host": host_val,
        "domain": domain_val,
        "risk_class": risk_class,
    }

    with engine.begin() as c:
        org = org_id or tool_policy_engine.org_of(c, project_id)
        rules = tool_policy_engine.load_rules(c, project_id, org)
        pctx = tool_policy_engine.PolicyContext(
            server="metatron",
            tool=tool,
            arguments=args_ctx,
            project=project_id,
            org=org,
            base="allow",
        )
        verdict = tool_policy_engine.decide(pctx, rules=rules)
        now = _now()
        matching_hard_rules = [
            r
            for r in (list(rules) + _builtin_hard_rule_rows())
            if bool(r.get("hard"))
            and str(r.get("status") or "active") == "active"
            and not tool_policy_engine._expired(r, now)
            and not tool_policy_engine._pin_stale(r, pctx)
            and tool_policy_engine.rule_matches(r, pctx)
        ]
        matched_hard = bool(matching_hard_rules)
        hard_denied = any(str(r.get("effect") or "") == "deny" for r in matching_hard_rules)
        is_hard = matched_hard or risk_class in HARD_RISK_CLASSES or tool in _BUILTIN_HARD_TOOLS
        if verdict["decision"] == "deny" or hard_denied:
            return {
                "decision": "deny",
                "reason": verdict.get("explanation") or "forbidden_by_policy",
                "reviewer_receipt_id": None,
                "hard": is_hard,
            }
        if is_hard:
            return {
                "decision": "escalate",
                "reason": "hard_rule_requires_human_approval",
                "reviewer_receipt_id": None,
                "hard": True,
            }

        grant_rows = c.execute(
            text(
                "SELECT * FROM metatron_grants "
                "WHERE principal_id = :p AND project_id = :proj "
                "AND revoked_at IS NULL AND expires_at > now() "
                "ORDER BY created_at DESC"
            ),
            {"p": principal_id, "proj": project_id},
        ).mappings().all()
        covering = next(
            (dict(g) for g in grant_rows if _grant_covers(g, tool, risk_class, call=call)),
            None,
        )
        if covering is None:
            return {
                "decision": "escalate",
                "reason": "no_active_grant",
                "reviewer_receipt_id": None,
            }

    from . import model_routing

    review_id = make_id("mrev")
    captured_receipt_id: Optional[str] = None

    def _envelope_run(produce: Any, *, prompt: str, system_prompt: str):
        nonlocal captured_receipt_id
        from .budget import BudgetGovernor
        from .jobs import JobLeaseService
        from .model_gateway import ModelGatewayService

        def commit(fn):
            with engine.begin() as conn:
                return fn(conn)

        spend = MetatronStepSpend(
            project_id=project_id,
            run_id=turn_id or review_id,
            step_id=f"review:{review_id}",
            attempt=1,
            actor=actor,
            worker_id=f"metatron-reviewer:{review_id}",
            budget=BudgetGovernor(settings),
            jobs=JobLeaseService(settings),
            gateway=ModelGatewayService(settings),
            receipts=ReceiptService(),
            commit=commit,
        )
        spend.acting = lineage.current() or lineage.Acting(
            principal_id=principal_id, org_id=org or ""
        )
        with engine.begin() as conn:
            adapter = model_routing.project_chat_adapter(conn, settings, project_id)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        msg, usage, rid = spend.run_chat_step(settings, adapter, messages=messages, tools=None)
        captured_receipt_id = rid or None
        text_out = str(msg.get("content") or "")
        if text_out:
            yield "chunk", text_out
        yield "done", (text_out, usage)

    candidates = [
        Candidate(
            key="approve",
            means="Approve this tool call within the operator's active grant.",
            acts=True,
            label="Approve",
        ),
        Candidate(
            key="deny",
            means="Deny this tool call because it violates safety or policy.",
            acts=True,
            label="Deny",
        ),
        Candidate(
            key="escalate",
            means="Escalate this tool call to ask the human operator directly.",
            acts=False,
            label="Escalate",
        ),
    ]
    situation = (
        f"Reviewing Metatron client tool call {tool!r} (risk_class={risk_class}, mode={mode}) "
        f"under active grant {covering['id']}."
    )
    question = (
        f"Should BOSS approve, deny, or escalate tool={tool!r} risk_class={risk_class!r} "
        f"argv={json.dumps(argv_redacted, default=str)} host={call.get('host')!r} "
        f"domain={call.get('domain')!r}?"
    )

    with engine.begin() as c:
        try:
            decision = AgentService(settings).decide(
                c,
                project_id=project_id,
                actor=actor,
                message=question,
                choices=candidates,
                situation=situation,
                external_ref=f"metatron_review:{session_id}:{review_id}",
                context={"call": dict(call), "grant_id": covering["id"], "mode": mode},
                envelope=SpendEnvelope(run=_envelope_run),
            )
        except MetatronBudgetError as exc:
            raise MetatronServerError(429, "budget_exhausted", exc.message) from exc
        except Exception as exc:  # noqa: BLE001
            return {
                "decision": "escalate",
                "reason": redaction.redact_secrets(str(exc))[:300],
                "reviewer_receipt_id": captured_receipt_id,
            }

    if decision.decided and decision.action in ("approve", "deny", "escalate"):
        chosen = decision.action
        if chosen == "approve" and is_hard:
            chosen = "escalate"
        return {
            "decision": chosen,
            "reason": decision.reason or "reviewed",
            "reviewer_receipt_id": captured_receipt_id,
        }
    return {
        "decision": "escalate",
        "reason": decision.reason or decision.error or "reviewer_escalated",
        "reviewer_receipt_id": captured_receipt_id,
    }


def _contains_credential_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        for k, v in value.items():
            if str(k).strip().lower() in FORBIDDEN_AUDIT_KEYS:
                return True
            if _contains_credential_payload(v):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_credential_payload(item) for item in value)
    if isinstance(value, str) and value:
        if redaction.mask_url_credentials(value) != value:
            return True
        for _label, pat in redaction._SECRET_PATTERNS:
            if pat.search(value):
                return True
    return False


_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_AUDIT_TEXT_FIELDS = (
    "session_id",
    "turn_id",
    "ticket_id",
    "kind",
    "tool",
    "risk_class",
    "host",
    "domain",
    "method",
    "path",
    "decision",
    "decided_by",
    "rule_id",
)


def _valid_pg_int(val: Any, *, min_val: int, max_val: int) -> bool:
    return isinstance(val, int) and not isinstance(val, bool) and min_val <= val <= max_val


def _validate_audit_event(raw: Mapping[str, Any]) -> Optional[str]:
    ts_raw = raw.get("ts")
    if ts_raw is not None and not isinstance(ts_raw, (str, datetime)):
        return "invalid_timestamp"
    for field in ("status", "exit_code", "duration_ms"):
        val = raw.get(field)
        if val is not None and not _valid_pg_int(val, min_val=_INT32_MIN, max_val=_INT32_MAX):
            return f"invalid_{field}"
    bytes_val = raw.get("bytes")
    if bytes_val is not None and not _valid_pg_int(bytes_val, min_val=_INT64_MIN, max_val=_INT64_MAX):
        return "invalid_bytes"
    cookie_names = raw.get("cookie_names")
    if cookie_names is not None:
        if not isinstance(cookie_names, (list, tuple)) or any(
            not isinstance(x, str) for x in cookie_names
        ):
            return "invalid_cookie_names"
    for field in _AUDIT_TEXT_FIELDS:
        val = raw.get(field)
        if val is not None and not isinstance(val, str):
            return f"invalid_{field}"
    return None


def ingest_audit_batch(
    connection: Any,
    *,
    org_id: str,
    project_id: str,
    principal_id: str,
    actor: str,
    session_id: str,
    events: list[Mapping[str, Any]],
) -> dict[str, Any]:
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            rejected.append({"event_id": "", "reason": "invalid_event_object"})
            continue
        raw_ev_id = raw.get("event_id")
        if raw_ev_id is not None and not isinstance(raw_ev_id, str):
            rejected.append({"event_id": "", "reason": "invalid_event_id"})
            continue
        ev_id = str(raw_ev_id or "").strip()
        if not ev_id:
            rejected.append({"event_id": "", "reason": "missing_event_id"})
            continue
        if _contains_credential_payload(raw):
            rejected.append({"event_id": ev_id, "reason": "credential_shaped_payload"})
            continue
        invalid_reason = _validate_audit_event(raw)
        if invalid_reason is not None:
            rejected.append({"event_id": ev_id, "reason": invalid_reason})
            continue
        ts_raw = raw.get("ts")
        if isinstance(ts_raw, datetime):
            ts = ts_raw if ts_raw.tzinfo is not None else ts_raw.replace(tzinfo=timezone.utc)
        elif isinstance(ts_raw, str) and ts_raw.strip():
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                rejected.append({"event_id": ev_id, "reason": "invalid_timestamp"})
                continue
        else:
            ts = _now()
        kind = str(raw.get("kind") or "tool_call").strip() or "tool_call"
        ticket_id = str(raw.get("ticket_id")).strip() if raw.get("ticket_id") else None
        cookie_names = (
            [str(x) for x in raw.get("cookie_names") or []]
            if raw.get("cookie_names") is not None
            else None
        )
        inserted = connection.execute(
            text(
                "INSERT INTO metatron_audit_events "
                "(event_id, org_id, project_id, session_id, turn_id, ticket_id, principal_id, "
                " ts, kind, tool, risk_class, argv_redacted, host, domain, cookie_names, "
                " method, path, status, decision, decided_by, rule_id, exit_code, bytes, duration_ms) "
                "VALUES (:eid, :o, :p, :sid, :tid, :tk, :pr, :ts, :k, :tool, :rc, "
                " CAST(:argv AS JSONB), :host, :dom, :cn, :m, :path, :st, :dec, :dby, :rid, "
                " :ec, :bytes, :dur) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING event_id"
            ),
            {
                "eid": ev_id,
                "o": org_id,
                "p": project_id,
                "sid": str(raw.get("session_id") or session_id),
                "tid": raw.get("turn_id"),
                "tk": ticket_id,
                "pr": principal_id,
                "ts": ts,
                "k": kind,
                "tool": raw.get("tool"),
                "rc": raw.get("risk_class"),
                "argv": (
                    json.dumps(raw["argv_redacted"], default=str)
                    if raw.get("argv_redacted") is not None
                    else None
                ),
                "host": raw.get("host"),
                "dom": raw.get("domain"),
                "cn": cookie_names,
                "m": raw.get("method"),
                "path": raw.get("path"),
                "st": raw.get("status"),
                "dec": raw.get("decision"),
                "dby": raw.get("decided_by"),
                "rid": raw.get("rule_id"),
                "ec": raw.get("exit_code"),
                "bytes": raw.get("bytes"),
                "dur": raw.get("duration_ms"),
            },
        ).scalar()
        accepted.append(ev_id)
        if inserted and ticket_id:
            emit_event(
                connection,
                project_id,
                "metatron.audit_attached",
                actor,
                "ticket",
                ticket_id,
                {
                    "event_id": ev_id,
                    "session_id": session_id,
                    "tool": raw.get("tool"),
                    "decision": raw.get("decision"),
                },
            )
    return {"accepted": accepted, "rejected": rejected}


def list_metatron_hosts(connection: Any, project_id: str) -> list[dict[str, Any]]:
    hosts = deployment_fleet.list_hosts(connection, project_id)
    out: list[dict[str, Any]] = []
    for h in hosts:
        out.append(
            {
                "id": h["id"],
                "project_id": h["project_id"],
                "name": h["name"],
                "address": h["address"],
                "ssh_user": h["ssh_user"],
                "ssh_port": int(h.get("ssh_port") or 22),
                "arch": h.get("arch"),
                "status": h.get("status"),
                "labels": _loads(h.get("labels"), {}),
                "key_configured": bool(h.get("key_secret_ref")),
                "host_key_known_hosts": h.get("host_key_known_hosts"),
                "host_key_fingerprint": h.get("host_key_fingerprint"),
            }
        )
    return out


def issue_host_credential_lease(
    connection: Any,
    settings: Settings,
    *,
    host_id: str,
    principal_id: str,
    actor: str,
    purpose: str,
    ttl_s: int,
    session_id: Optional[str],
) -> dict[str, Any]:
    if not 15 <= int(ttl_s) <= 300:
        raise MetatronServerError(422, "invalid_ttl", "ttl_s must be between 15 and 300 seconds.")
    if not str(purpose or "").strip():
        raise MetatronServerError(422, "invalid_purpose", "purpose is required.")

    host = connection.execute(
        text("SELECT * FROM deployment_hosts WHERE id = :id"), {"id": host_id}
    ).mappings().first()
    if not host:
        raise MetatronServerError(404, "host_not_found", f"Host {host_id} was not found.")
    if host["status"] == "retired":
        raise MetatronServerError(409, "host_retired", f"Host {host_id} is retired.")
    secret_ref_id = host.get("key_secret_ref")
    if not secret_ref_id:
        raise MetatronServerError(
            409, "host_key_missing", f"Host {host_id} has no configured SSH key secret reference."
        )

    scope = connection.execute(
        text(
            "SELECT w.org_id, p.workspace_id FROM projects p "
            "JOIN workspaces w ON w.id = p.workspace_id WHERE p.id = :p"
        ),
        {"p": host["project_id"]},
    ).mappings().first()
    if not scope:
        raise MetatronServerError(404, "project_not_found", "Host project scope not found.")

    try:
        material = secret_backends.resolve_secret_ref(
            connection,
            str(secret_ref_id),
            settings,
            project_id=host["project_id"],
            principal=principal_id,
        )
    except secret_backends.SecretUnavailable as exc:
        raise MetatronServerError(409, "secret_unavailable", str(exc)) from exc

    lease_id = make_id("mcl")
    expires_at = _now() + timedelta(seconds=int(ttl_s))
    receipt, _ = ReceiptService().record_completed(
        connection,
        str(scope["org_id"]),
        str(scope["workspace_id"]),
        str(host["project_id"]),
        f"metatron-cred-lease:{lease_id}",
        "metatron.credential_lease",
        {"host_id": host_id, "principal_id": principal_id},
        {"lease_id": lease_id, "host_id": host_id, "purpose": purpose[:200], "ttl_seconds": int(ttl_s)},
        provider_ref=f"vault:{secret_ref_id}",
    )
    connection.execute(
        text(
            "INSERT INTO metatron_credential_leases "
            "(id, org_id, project_id, host_id, secret_ref_id, principal_id, session_id, "
            " purpose, ttl_seconds, expires_at, redeemed_at, receipt_id) "
            "VALUES (:id, :o, :p, :h, :sr, :pr, :sid, :pur, :ttl, :exp, now(), :rid)"
        ),
        {
            "id": lease_id,
            "o": str(scope["org_id"]),
            "p": str(host["project_id"]),
            "h": host_id,
            "sr": str(secret_ref_id),
            "pr": principal_id,
            "sid": session_id,
            "pur": purpose[:400],
            "ttl": int(ttl_s),
            "exp": expires_at,
            "rid": receipt["id"],
        },
    )
    emit_event(
        connection,
        str(host["project_id"]),
        "metatron.credential_lease_issued",
        actor,
        "deployment_host",
        host_id,
        {"lease_id": lease_id, "principal_id": principal_id, "ttl_seconds": int(ttl_s)},
    )
    return {
        "lease_id": lease_id,
        "host_id": host_id,
        "kind": "ssh_key",
        "expires_at": expires_at.isoformat(),
        "host_key_known_hosts": host.get("host_key_known_hosts"),
        "host_key_fingerprint": host.get("host_key_fingerprint"),
        "material": material,
    }
