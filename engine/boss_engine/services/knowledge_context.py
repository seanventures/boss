"""Knowledge / context packing for operators and agents (BOSS-356).

Exposes ``ToolchainRegistryService.pack_context`` as a knowledge-plane helper:
accepts messages or context items, optionally injects tool catalog snippets,
redacts secrets before packing, and never returns raw secret material in the
packed context.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping, Optional

from .toolchain_registry import (
    AUTHORITATIVE_CONTEXT,
    MAX_CONTEXT_CHUNKS,
    ToolchainImportError,
    ToolchainRegistryService,
)

# Default input budget for agent/operator packing when the client omits one.
DEFAULT_TOKEN_BUDGET = 8192
MIN_TOKEN_BUDGET = 128
MAX_TOKEN_BUDGET = 2_000_000

# Credential-shaped patterns only (aligned with redaction.py secret labels).
# Broader PII is intentionally not stripped so operational context stays useful.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "SECRET",
        re.compile(r"\b(?:sk|pk|rk|whsec|ghp|gho|xox[baprs])[-_](?:(?:live|test|proj)[-_])?[A-Za-z0-9]{16,}\b"),
    ),
    (
        "BEARER",
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    ),
    ("GITHUB_TOKEN", re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{22,}|gh[usr]_[A-Za-z0-9]{20,})\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("SLACK_WEBHOOK", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    # Credentials in a URL (scheme://user:password@host): the password part only.
    ("URL_PASSWORD", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]{3,}(?=@)")),
    # An assignment of a secret-named setting: password=..., "api_key": "...", AWS_SECRET_ACCESS_KEY=...
    ("ASSIGNED_SECRET", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|token|private[_-]?key)"
        r"[A-Za-z0-9_]*[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{6,}")),
    (
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
)


def knowledge_status(*, project_id: Optional[str] = None) -> dict[str, Any]:
    """Lightweight availability probe for knowledge packing."""
    return {
        "project_id": project_id,
        "pack_context_available": True,
        "default_token_budget": DEFAULT_TOKEN_BUDGET,
        "min_token_budget": MIN_TOKEN_BUDGET,
        "max_token_budget": MAX_TOKEN_BUDGET,
        "max_chunks": MAX_CONTEXT_CHUNKS,
        "authoritative_authorities": sorted(AUTHORITATIVE_CONTEXT),
        "redaction_enabled": True,
        "backend": "toolchain.context.pack",
    }


def pack_knowledge_context(
    *,
    messages: Optional[Iterable[Any]] = None,
    items: Optional[Iterable[Any]] = None,
    chunks: Optional[Iterable[Any]] = None,
    token_budget: Optional[int] = None,
    reserved_output_tokens: int = 0,
    tool_ids: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Normalize, redact, optionally attach tools, then pack under a token budget.

    Returns the pack_context result plus ``redaction_summary`` and
    ``tools_attached`` metadata. Secrets matched by the PII/secret redactor are
    replaced with ``[LABEL]`` markers before packing so they never appear in
    ``context``.
    """
    budget = DEFAULT_TOKEN_BUDGET if token_budget is None else int(token_budget)
    normalized, redaction_summary = _normalize_and_redact(
        messages=messages,
        items=items,
        chunks=chunks,
    )
    tools_attached: list[str] = []
    if tool_ids:
        tool_chunks, attached = _tool_chunks(tool_ids)
        tools_attached = attached
        # Tool catalog is reference material; append after caller context.
        normalized.extend(tool_chunks)

    if not normalized:
        raise ToolchainImportError(
            "At least one message, item, or chunk with text is required."
        )

    result = ToolchainRegistryService.pack_context(
        normalized,
        budget,
        int(reserved_output_tokens or 0),
    )
    # Defense in depth: re-redact the rendered context string.
    packed_context, post_summary = _redact_secrets(str(result.get("context") or ""))
    if post_summary:
        for label, count in post_summary.items():
            redaction_summary[label] = redaction_summary.get(label, 0) + int(count)
        result = {**result, "context": packed_context}

    result["redaction_summary"] = redaction_summary
    result["tools_attached"] = tools_attached
    result["token_budget"] = budget
    result["default_token_budget"] = DEFAULT_TOKEN_BUDGET
    return result


def _redact_secrets(text: str) -> tuple[str, dict[str, int]]:
    """Replace credential-shaped secrets with ``[LABEL]`` markers."""
    if not text:
        return text, {}
    summary: dict[str, int] = {}
    out = text
    for label, pattern in _SECRET_PATTERNS:
        count = 0

        def _sub(_match: re.Match[str], _label: str = label) -> str:
            nonlocal count
            count += 1
            return f"[{_label}]"

        out = pattern.sub(_sub, out)
        if count:
            summary[label] = count
    return out, summary


def _normalize_and_redact(
    *,
    messages: Optional[Iterable[Any]],
    items: Optional[Iterable[Any]],
    chunks: Optional[Iterable[Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    summary: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    index = 0
    for raw in _iter_sources(messages, items, chunks):
        chunk = _coerce_item(raw, index)
        index += 1
        if chunk is None:
            continue
        clean, item_summary = _redact_secrets(chunk["text"])
        for label, count in item_summary.items():
            summary[label] = summary.get(label, 0) + int(count)
        chunk["text"] = clean
        if not chunk["text"].strip():
            continue
        out.append(chunk)
    return out, summary


def _iter_sources(
    messages: Optional[Iterable[Any]],
    items: Optional[Iterable[Any]],
    chunks: Optional[Iterable[Any]],
) -> list[Any]:
    merged: list[Any] = []
    for source in (messages, items, chunks):
        if source is None:
            continue
        if not isinstance(source, list):
            raise ToolchainImportError("messages, items, and chunks must be arrays.")
        merged.extend(source)
    return merged


def _coerce_item(raw: Any, index: int) -> Optional[dict[str, Any]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        body = raw.strip()
        if not body:
            return None
        return {
            "id": f"chunk-{index}",
            "text": body,
            "authority": "reference",
            "required": False,
            "priority": 50,
            "source_ref": None,
        }
    if not isinstance(raw, Mapping):
        raise ToolchainImportError("Every context item must be an object or string.")

    # Message-style: {role, content} or item-style: {id, text, ...}
    body = raw.get("text")
    if body is None:
        body = raw.get("content")
    body = str(body or "").strip()
    if not body:
        return None

    role = str(raw.get("role") or "").strip().lower()
    chunk_id = str(raw.get("id") or "").strip()
    if not chunk_id:
        chunk_id = f"{role}-{index}" if role else f"chunk-{index}"

    authority = str(raw.get("authority") or "reference").strip().lower()
    if role in {"system", "developer"} and authority == "reference":
        # System prompts are higher authority by default.
        authority = "policy" if role == "system" else "contract"

    source_ref = raw.get("source_ref")
    if source_ref is None and role:
        source_ref = f"message:{role}"

    priority = raw.get("priority", 50)
    try:
        priority_int = int(priority)
    except (TypeError, ValueError) as error:
        raise ToolchainImportError("priority must be an integer.") from error

    return {
        "id": chunk_id,
        "text": body,
        "authority": authority,
        "required": bool(raw.get("required")) or authority in AUTHORITATIVE_CONTEXT,
        "priority": priority_int,
        "source_ref": str(source_ref).strip() if source_ref else None,
    }


def _tool_chunks(tool_ids: Iterable[str]) -> tuple[list[dict[str, Any]], list[str]]:
    requested = [str(tid).strip() for tid in tool_ids if str(tid or "").strip()]
    if not requested:
        return [], []
    if len(requested) > 100:
        raise ToolchainImportError("tool_ids exceeds the 100-item limit.")

    catalog = _tool_catalog()
    by_name = {str(item.get("name") or ""): item for item in catalog}
    chunks: list[dict[str, Any]] = []
    attached: list[str] = []
    for tool_id in requested:
        entry = by_name.get(tool_id)
        if entry is None:
            # Soft-miss: attach a stub so agents see the gap without failing the pack.
            body = f"Tool '{tool_id}' is not in the native catalog."
            chunks.append(
                {
                    "id": f"tool:{tool_id}",
                    "text": body,
                    "authority": "reference",
                    "required": False,
                    "priority": 10,
                    "source_ref": f"tool:{tool_id}",
                }
            )
            continue
        body = json.dumps(
            {
                "name": entry.get("name"),
                "kind": entry.get("kind"),
                "risk_tier": entry.get("risk_tier"),
                "description": entry.get("description"),
                "parameters": entry.get("parameters"),
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        chunks.append(
            {
                "id": f"tool:{tool_id}",
                "text": body,
                "authority": "reference",
                "required": False,
                "priority": 40,
                "source_ref": f"tool:{tool_id}",
            }
        )
        attached.append(tool_id)
    return chunks, attached


def _tool_catalog() -> list[dict[str, Any]]:
    try:
        from .agent_loop import describe_native_tools

        return list(describe_native_tools() or [])
    except Exception:
        # Catalog is optional enrichment; packing still works without it.
        return []
