"""Deterministic natural-language process compiler.

Turns operator prose into a process spec that `validate_process_spec` accepts.
No LLM, no network, no spend. Unknown roles and actions fail closed.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .agent_process import PRD_TO_ACCEPTED_SPEC, validate_process_spec


DEFAULT_PANEL_ROLES = ["chief_architect", "cto", "qa", "ux_engineer"]

# Longest phrases first so "chief architect" wins over "architect".
ROLE_PHRASES: tuple[tuple[str, str], ...] = (
    ("chief architect", "chief_architect"),
    ("engineering captain", "engineering_captain"),
    ("product manager", "product_manager"),
    ("vp product", "vp_product"),
    ("vp pm", "vp_product"),
    ("ux engineer", "ux_engineer"),
    ("ui/ux", "ux_engineer"),
    ("ui ux", "ux_engineer"),
    ("architect", "chief_architect"),
    ("captain", "engineering_captain"),
    ("cto", "cto"),
    ("vp", "vp_product"),
    ("pm", "product_manager"),
    ("qa", "qa"),
    ("ux", "ux_engineer"),
    ("ui", "ux_engineer"),
)

# "panel" is a kind, not a ROLES member.
KNOWN_ACTOR_PHRASES: tuple[tuple[str, str], ...] = ROLE_PHRASES + (("panel", "panel"),)

ACTION_PHRASES: tuple[tuple[str, str], ...] = (
    ("accepted work", "accept_work"),
    ("accept work", "accept_work"),
    ("deliver prd", "accept_prd"),
    ("delivered prd", "accept_prd"),
    ("prd delivered", "accept_prd"),
    ("prd is delivered", "accept_prd"),
    ("accept prd", "accept_prd"),
    ("accept the prd", "accept_prd"),
    ("deliver the prd", "accept_prd"),
    ("ui/ux review", "ux_review"),
    ("ui review", "ux_review"),
    ("ux review", "ux_review"),
    ("qa review", "qa_review"),
    ("review prd", "review_prd"),
    ("improve prd", "improve_prd"),
    ("certify spec", "certify_spec"),
    ("panel certify", "certify_spec"),
    ("panel certified", "certify_spec"),
    ("buildout", "build"),
    ("certified", "certify_spec"),
    ("certify", "certify_spec"),
    ("verified", "verify_build"),
    ("verify", "verify_build"),
    ("improved", "improve_prd"),
    ("improve", "improve_prd"),
    ("reviewed", "review_prd"),
    ("review", "review_prd"),
    ("built", "build"),
    ("build", "build"),
    ("tested", "qa_review"),
    ("test", "qa_review"),
    ("closed", "accept_work"),
    ("close", "accept_work"),
    ("accepted", "accept_work"),
    ("accept", "accept_work"),
    ("delivered", "accept_prd"),
    ("deliver", "accept_prd"),
)

KNOWN_ACTION_STEMS = frozenset({
    "accept", "accepted", "accepts",
    "deliver", "delivered", "delivers", "delivery",
    "review", "reviewed", "reviews", "reviewing",
    "improve", "improved", "improves", "improvement",
    "build", "built", "builds", "buildout", "building",
    "qa", "test", "tests", "tested", "testing",
    "ux", "ui",
    "verify", "verified", "verifies", "verification",
    "certify", "certified", "certifies", "certification",
    "panel",
    "close", "closed", "closes",
    "check", "checked", "checks",
    "work", "prd",
})

UNKNOWN_ACTION_RE = re.compile(
    r"\b(deploys?|launches?|deletes?|destroys?|merges?|publishes?|rollbacks?|"
    r"scales?|provisions?|executes?|ships?|releases?|migrates?|refunds?|"
    r"restarts?|kills?|terminates?|invoices?)\b"
)

FILLER = frozenset({
    "a", "an", "the", "to", "of", "for", "and", "or", "by", "in", "on", "at",
    "is", "are", "was", "were", "be", "been", "being", "it", "its", "this",
    "that", "we", "they", "our", "their", "then", "than", "next", "after",
    "before", "when", "once", "with", "from", "into", "as", "if", "so",
    "who", "whom", "which", "what", "will", "shall", "must", "can", "may",
    "prd", "work", "process", "step", "steps", "flow", "chain", "handoff",
    "handoffs", "story", "default", "custom", "first", "finally", "last",
    "also", "checked", "check", "delivery", "intake", "user", "users",
    "operator", "team", "teams", "role", "roles", "agent", "agents", "spec",
    "specs", "text", "command", "human", "owner", "each", "both", "all",
    "every", "same", "other", "another", "more", "via", "using", "use",
    "used", "gets", "get", "got", "does", "do", "done", "has", "have",
    "had", "not", "no", "yes", "new", "one", "parallel", "sequence",
    "sequential", "join", "fan", "out", "wait", "until", "while", "during",
    "over", "under", "between", "among", "service", "app", "software",
    "factory", "thing", "item", "part", "the",
})

CANONICAL_IDS: dict[tuple[str, str, str], str] = {
    ("product_manager", "accept_prd", "role"): "intake_prd",
    ("chief_architect", "review_prd", "role"): "review_architect",
    ("cto", "review_prd", "role"): "review_cto",
    ("vp_product", "review_prd", "role"): "review_vp",
    ("product_manager", "improve_prd", "role"): "improve_prd",
    ("engineering_captain", "build", "role"): "captain_build",
    ("qa", "qa_review", "role"): "qa_check",
    ("ux_engineer", "ux_review", "role"): "ux_review",
    ("engineering_captain", "verify_build", "role"): "captain_verify",
    ("engineering_captain", "accept_work", "role"): "captain_accept",
    ("panel", "certify_spec", "panel"): "panel_certify",
}

DEFAULT_STORY_PATTERNS = (
    r"\bprd\b",
    r"\b(architect|chief architect)\b",
    r"\bcto\b",
    r"\b(vp product|vp pm|\bvp\b)\b",
    r"\bimprov",
    r"\b(build|built|buildout)\b",
    r"\bqa\b",
    r"\b(ui/ux|ui ux|\bux\b|\bui\b)\b",
    r"\bverif",
    r"\b(panel|certif)",
    r"\b(accept|accepted|close|closed)\b",
    r"\b(captain|engineering captain)\b",
)

_ROLE_START = (
    r"(?:the\s+)?(?:chief architect|engineering captain|product manager|"
    r"vp product|vp pm|ux engineer|ui/ux|ui ux|architect|captain|cto|vp|"
    r"pm|qa|ux|ui|panel)\b"
)
_COMMA_STEP_RE = re.compile(
    rf"\s*,\s*(?=(?:{_ROLE_START}|improv\w*|build\w*|verif\w*|certif\w*|"
    rf"accept\w*|close\w*|qa\b|test\w*|ui/ux|ux\b))"
)
_AND_STEP_RE = re.compile(rf"\s+\band\b\s+(?={_ROLE_START})")
_ACTOR_BEFORE_ACTION = re.compile(
    r"\b([a-z][a-z0-9_+/-]{1,40})\b(?=\s+(?:reviews?|reviewed|builds?|built|"
    r"tests?|tested|certif(?:y|ies|ied)|accepts?|accepted|improves?|improved|"
    r"verif(?:y|ies|ied)|delivers?|delivered|checks?|checked|closes?|closed|"
    r"deploys?|deployed|launches?|launched|ships?|shipped|merges?|merged)\b)"
)


def compile_process_from_text(text: str, name: str | None = None) -> dict[str, Any]:
    """Compile operator prose into a validated process spec."""
    if not (text or "").strip():
        raise ValueError("process text is empty")
    normalized = _normalize(text)
    _assert_known_vocabulary(normalized)
    if _is_default_story(normalized):
        spec = validate_process_spec(copy.deepcopy(PRD_TO_ACCEPTED_SPEC))
    else:
        stages = _parse_stages(normalized)
        if not stages:
            raise ValueError("no process steps found")
        spec = validate_process_spec(_stages_to_spec(stages, text))
    if name and str(name).strip():
        spec["name"] = str(name).strip()[:160]
    return spec


def compile_process(text: str, name: str | None = None) -> dict[str, Any]:
    """Alias used by IntentRouter / Metatron."""
    return compile_process_from_text(text, name=name)


def compile_preview(text: str) -> dict[str, Any]:
    """Return {spec, warnings} for authoring UI. Never invents scores."""
    spec = compile_process_from_text(text)
    warnings: list[str] = []
    if spec.get("key") == PRD_TO_ACCEPTED_SPEC["key"]:
        warnings.append("reused default PRD-to-accepted-work spec")
    return {"spec": spec, "warnings": warnings}


def _normalize(text: str) -> str:
    lowered = text.strip().lower()
    lowered = re.sub(r"\bui\s*/\s*ux\b", "ui/ux", lowered)
    lowered = re.sub(r"\baccept\s*/\s*deliver\b", "deliver", lowered)
    lowered = re.sub(r"panel[- ]certified", "panel certified", lowered)
    lowered = re.sub(r"panel[- ]certify", "panel certify", lowered)
    lowered = re.sub(r"\band then\b", " then ", lowered)
    lowered = re.sub(r"[.;]+", " then ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _is_default_story(normalized: str) -> bool:
    return all(re.search(pattern, normalized) for pattern in DEFAULT_STORY_PATTERNS)


def _replace_phrases(text: str, phrases: tuple[tuple[str, str], ...], token: str) -> str:
    out = text
    for phrase, _role in phrases:
        out = re.sub(rf"\b{re.escape(phrase)}\b", token, out)
    return out


def _assert_known_vocabulary(normalized: str) -> None:
    unknown_action = UNKNOWN_ACTION_RE.search(normalized)
    if unknown_action:
        raise ValueError(f"unknown action {unknown_action.group(1)!r}")
    scrubbed = _replace_phrases(normalized, KNOWN_ACTOR_PHRASES, "ROLE")
    for match in _ACTOR_BEFORE_ACTION.finditer(scrubbed):
        actor = match.group(1)
        if actor in FILLER or actor == "role":
            continue
        raise ValueError(f"unknown role {actor!r}")
    for match in re.finditer(r"\bROLE\s+([a-z][a-z-]*)\b", scrubbed):
        verb = match.group(1)
        if verb in KNOWN_ACTION_STEMS or verb in FILLER:
            continue
        raise ValueError(f"unknown action {verb!r}")


def _find_phrases(text: str, phrases: tuple[tuple[str, str], ...]) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for phrase, mapped in phrases:
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", text):
            if any(match.start() < end and match.end() > start for start, end, _ in found):
                continue
            found.append((match.start(), match.end(), mapped))
    found.sort(key=lambda item: item[0])
    return found


def _extract_roles(text: str) -> list[str]:
    roles: list[str] = []
    for _start, _end, role in _find_phrases(text, ROLE_PHRASES):
        if role not in roles:
            roles.append(role)
    return roles


def _extract_actions(text: str) -> list[str]:
    return [mapped for _start, _end, mapped in _find_phrases(text, ACTION_PHRASES)]


def _has_panel(text: str) -> bool:
    return bool(re.search(r"\bpanel\b", text)) or bool(re.search(r"\bcertif", text))


def _is_parallel_clause(clause: str, roles: list[str], actions: list[str]) -> bool:
    if len(roles) < 2:
        return False
    if re.search(r"\bin parallel\b", clause):
        return True
    if re.search(r"\b(checked|reviewed|review|reviewing)\s+by\b", clause):
        review_roles = {"chief_architect", "cto", "vp_product", "product_manager"}
        return all(role in review_roles for role in roles)
    if len(set(actions)) <= 1 and set(roles) <= {"chief_architect", "cto", "vp_product"}:
        return True
    return False


def _action_for_role(
    role: str,
    clause: str,
    explicit: list[str],
    index: int,
    total: int,
) -> str:
    if role == "qa" and (
        not explicit or set(explicit) <= {"qa_review", "review_prd"} or "check" in clause
    ):
        return "qa_review"
    if role == "ux_engineer" and (
        not explicit or set(explicit) <= {"ux_review", "review_prd"} or "check" in clause
    ):
        return "ux_review"
    preferred = {
        "chief_architect": "review_prd",
        "cto": "review_prd",
        "vp_product": "review_prd",
    }
    if role in preferred and (
        not explicit or set(explicit) <= {"review_prd"} or re.search(r"\b(check|review)", clause)
    ):
        return preferred[role]
    if explicit:
        return _disambiguate_action(explicit[0], clause, index, total)
    if role == "product_manager":
        if re.search(r"\bimprov", clause) or index > 0:
            return "improve_prd"
        return "accept_prd"
    if role == "engineering_captain":
        if re.search(r"\bverif", clause):
            return "verify_build"
        if re.search(r"\b(accept|close)\b", clause) or index == total - 1:
            return "accept_work"
        return "build"
    if role == "qa":
        return "qa_review"
    if role == "ux_engineer":
        return "ux_review"
    raise ValueError(f"unknown action for role {role!r}")


def _disambiguate_action(action: str, clause: str, index: int, total: int) -> str:
    if action in {"accept_prd", "accept_work"}:
        if re.search(r"\bprd\b", clause) and not re.search(r"\bwork\b", clause):
            return "accept_prd"
        if index == 0 and re.search(r"\b(prd|deliver)", clause):
            return "accept_prd"
        if index == total - 1 or re.search(r"\b(work|close)\b", clause):
            return "accept_work"
        return "accept_prd" if index == 0 else "accept_work"
    return action


def _default_role_for_action(action: str) -> str | None:
    return {
        "accept_prd": "product_manager",
        "improve_prd": "product_manager",
        "build": "engineering_captain",
        "qa_review": "qa",
        "ux_review": "ux_engineer",
        "verify_build": "engineering_captain",
        "accept_work": "engineering_captain",
    }.get(action)


def _split_clauses(normalized: str) -> list[str]:
    chunks = [part.strip(" ,") for part in re.split(r"\bthen\b", normalized)]
    clauses: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        roles = _extract_roles(chunk)
        actions = _extract_actions(chunk)
        if _is_parallel_clause(chunk, roles, actions):
            clauses.append(chunk)
            continue
        pieces = [part.strip(" ,") for part in _COMMA_STEP_RE.split(chunk) if part.strip(" ,")]
        for piece in pieces or [chunk]:
            if _is_parallel_clause(piece, _extract_roles(piece), _extract_actions(piece)):
                clauses.append(piece)
                continue
            and_parts = [part.strip(" ,") for part in _AND_STEP_RE.split(piece) if part.strip(" ,")]
            clauses.extend(and_parts or [piece])
    return [clause for clause in clauses if clause]


def _clause_to_stages(clause: str, index: int, total: int) -> list[dict[str, Any]]:
    roles = _extract_roles(clause)
    explicit = _extract_actions(clause)
    if _has_panel(clause) and (
        not roles or re.search(r"\b(panel|certif)", clause)
    ) and not (roles and not re.search(r"\b(panel|certif)", clause)):
        if re.search(r"\b(panel|certif)", clause):
            panel_roles = [role for role in roles if role in DEFAULT_PANEL_ROLES] or list(
                DEFAULT_PANEL_ROLES
            )
            return [{
                "kind": "panel",
                "roles": panel_roles,
                "action": "certify_spec",
            }]
    if _is_parallel_clause(clause, roles, explicit):
        action = explicit[0] if explicit else "review_prd"
        action = _disambiguate_action(action, clause, index, total)
        if action not in {"review_prd", "qa_review", "ux_review"}:
            action = "review_prd"
        return [{
            "kind": "role",
            "parallel": True,
            "roles": roles,
            "action": action,
        }]
    if not roles:
        if not explicit:
            return []
        action = _disambiguate_action(explicit[0], clause, index, total)
        if action == "certify_spec":
            return [{
                "kind": "panel",
                "roles": list(DEFAULT_PANEL_ROLES),
                "action": "certify_spec",
            }]
        role = _default_role_for_action(action)
        if role is None:
            raise ValueError(f"unknown role for action {action!r}")
        return [{"kind": "role", "role": role, "action": action, "parallel": False}]
    stages: list[dict[str, Any]] = []
    inferred = [
        (role, _action_for_role(role, clause, explicit, index, total))
        for role in roles
    ]
    unique_actions = {action for _role, action in inferred}
    if len(inferred) > 1 and len(unique_actions) == 1 and (
        _is_parallel_clause(clause, roles, list(unique_actions))
        or unique_actions == {"review_prd"}
    ):
        return [{
            "kind": "role",
            "parallel": True,
            "roles": [role for role, _action in inferred],
            "action": next(iter(unique_actions)),
        }]
    for role, action in inferred:
        stages.append({
            "kind": "role",
            "role": role,
            "action": action,
            "parallel": False,
        })
    return stages


def _parse_stages(normalized: str) -> list[dict[str, Any]]:
    clauses = _split_clauses(normalized)
    stages: list[dict[str, Any]] = []
    for index, clause in enumerate(clauses):
        stages.extend(_clause_to_stages(clause, index, len(clauses)))
    return stages


def _step_id(role: str, action: str, kind: str, used: set[str]) -> str:
    base = CANONICAL_IDS.get((role, action, kind))
    if base is None:
        base = f"panel_{action}" if kind == "panel" else f"{role}_{action}"
    step_id = base
    suffix = 2
    while step_id in used:
        step_id = f"{base}_{suffix}"
        suffix += 1
    used.add(step_id)
    return step_id


def _stages_to_spec(stages: list[dict[str, Any]], source_text: str) -> dict[str, Any]:
    used: set[str] = set()
    groups: list[list[dict[str, Any]]] = []
    for stage in stages:
        if stage.get("kind") == "panel":
            groups.append([{
                "id": _step_id("panel", stage["action"], "panel", used),
                "kind": "panel",
                "roles": list(stage.get("roles") or DEFAULT_PANEL_ROLES),
                "action": stage["action"],
                "next": [],
                "wait_for": [],
            }])
            continue
        if stage.get("parallel"):
            group = []
            for role in stage["roles"]:
                group.append({
                    "id": _step_id(role, stage["action"], "role", used),
                    "kind": "role",
                    "role": role,
                    "action": stage["action"],
                    "next": [],
                    "wait_for": [],
                })
            groups.append(group)
            continue
        groups.append([{
            "id": _step_id(stage["role"], stage["action"], "role", used),
            "kind": "role",
            "role": stage["role"],
            "action": stage["action"],
            "next": [],
            "wait_for": [],
        }])
    for index, group in enumerate(groups):
        if index + 1 >= len(groups):
            continue
        nxt = [step["id"] for step in groups[index + 1]]
        for step in group:
            step["next"] = list(nxt)
        if len(group) > 1:
            for step in groups[index + 1]:
                step["wait_for"] = [item["id"] for item in group]
    steps = [step for group in groups for step in group]
    return {
        "key": "custom",
        "name": "Custom process",
        "description": source_text.strip()[:1000],
        "entry": steps[0]["id"],
        "steps": steps,
    }
