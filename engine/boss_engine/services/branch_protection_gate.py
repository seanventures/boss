"""Safety gate: protect canonical boss-sandbox main before live PR demos (BOSS-324).

Fail closed: ``live_demo_ready`` is false unless either:

1. Live GitHub default-branch protection meets the required checklist (via adapter
   when credentials work), or
2. An explicit **simulated** checklist has been stored for tests / dry-run
   rehearsals (never treated as production protection).

GitHub private-repo protection may return HTTP 403 on free plans; that is an
honest blocker, not a pass.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import uuid4

# Canonical requirements for boss-sandbox main before a live PR demo.
# Required status checks are intentionally omitted when Actions are disabled.
REQUIRED_CHECKS: list[dict[str, Any]] = [
    {
        "id": "protected",
        "label": "Default branch is protected",
        "required": True,
    },
    {
        "id": "required_approving_review_count_ge_1",
        "label": "At least one approving review required",
        "required": True,
    },
    {
        "id": "dismiss_stale_reviews",
        "label": "Dismiss stale reviews on new commits",
        "required": True,
    },
    {
        "id": "require_conversation_resolution",
        "label": "Require conversation resolution before merge",
        "required": True,
    },
    {
        "id": "enforce_admins",
        "label": "Include administrators in protection rules",
        "required": True,
    },
    {
        "id": "allow_force_pushes_false",
        "label": "Force pushes disabled",
        "required": True,
    },
    {
        "id": "allow_deletions_false",
        "label": "Branch deletions disabled",
        "required": True,
    },
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def dry_run_checklist() -> list[dict[str, Any]]:
    """Static checklist for operators when credentials are unavailable."""
    return [
        {
            **item,
            "status": "unchecked",
            "evidence": None,
            "how": (
                "Configure on GitHub → Settings → Branches → Branch protection rules "
                "for the default branch, or PUT /repos/{owner}/{repo}/branches/"
                "{branch}/protection with the matching fields."
            ),
        }
        for item in REQUIRED_CHECKS
    ]


def evaluate_protection_payload(
    protection: Optional[dict[str, Any]],
    *,
    protected_flag: Optional[bool] = None,
) -> dict[str, Any]:
    """Pure evaluation of a GitHub branch protection JSON body (or adapter snapshot).

    Accepts either the full protection API payload or the thinner
    ``branch_snapshot()['protection']`` shape from GitHubAdapter.
    """
    protection = protection or {}
    reviews = protection.get("required_pull_request_reviews") or {}
    if not isinstance(reviews, dict):
        reviews = {}

    enforce = protection.get("enforce_admins")
    if isinstance(enforce, dict):
        enforce_enabled = bool(enforce.get("enabled"))
    else:
        enforce_enabled = bool(enforce) if enforce is not None else False

    allow_force = protection.get("allow_force_pushes")
    if isinstance(allow_force, dict):
        force_allowed = bool(allow_force.get("enabled"))
    elif allow_force is None:
        force_allowed = False  # GitHub default when omitted after protect
    else:
        force_allowed = bool(allow_force)

    allow_del = protection.get("allow_deletions")
    if isinstance(allow_del, dict):
        deletions_allowed = bool(allow_del.get("enabled"))
    elif allow_del is None:
        deletions_allowed = False
    else:
        deletions_allowed = bool(allow_del)

    # Conversation resolution may appear as top-level bool or nested
    require_conversation = protection.get("required_conversation_resolution")
    if isinstance(require_conversation, dict):
        conversation_ok = bool(require_conversation.get("enabled"))
    elif require_conversation is None:
        conversation_ok = False
    else:
        conversation_ok = bool(require_conversation)

    approving = reviews.get("required_approving_review_count")
    try:
        approving_n = int(approving) if approving is not None else 0
    except (TypeError, ValueError):
        approving_n = 0

    dismiss_stale = bool(reviews.get("dismiss_stale_reviews"))

    enabled = protection.get("enabled")
    is_protected = (
        bool(protected_flag)
        if protected_flag is not None
        else bool(enabled if enabled is not None else protection)
    )
    # Empty protection dict with protected_flag false → not protected
    if not protection and protected_flag is not True:
        is_protected = False

    results: list[dict[str, Any]] = []

    def _check(check_id: str, ok: bool, evidence: Any) -> None:
        meta = next(c for c in REQUIRED_CHECKS if c["id"] == check_id)
        results.append(
            {
                "id": check_id,
                "label": meta["label"],
                "required": True,
                "ok": bool(ok),
                "evidence": evidence,
            }
        )

    _check("protected", is_protected, {"protected": is_protected})
    _check(
        "required_approving_review_count_ge_1",
        approving_n >= 1,
        {"required_approving_review_count": approving_n},
    )
    _check(
        "dismiss_stale_reviews",
        dismiss_stale,
        {"dismiss_stale_reviews": dismiss_stale},
    )
    _check(
        "require_conversation_resolution",
        conversation_ok,
        {"required_conversation_resolution": conversation_ok},
    )
    _check(
        "enforce_admins",
        enforce_enabled,
        {"enforce_admins": enforce_enabled},
    )
    _check(
        "allow_force_pushes_false",
        not force_allowed,
        {"allow_force_pushes": force_allowed},
    )
    _check(
        "allow_deletions_false",
        not deletions_allowed,
        {"allow_deletions": deletions_allowed},
    )

    failed = [r for r in results if not r["ok"]]
    all_ok = len(failed) == 0 and is_protected
    return {
        "checks": results,
        "passed": all_ok,
        "failed_ids": [r["id"] for r in failed],
        "summary": (
            "all_required_checks_passed"
            if all_ok
            else f"failed:{','.join(r['id'] for r in failed)}"
        ),
    }


# Process-local simulated checklists (tests / dry-run). Keyed by surface_key.
_sim_lock = threading.Lock()
_simulated: dict[str, dict[str, Any]] = {}


def reset_simulated_for_tests() -> None:
    with _sim_lock:
        _simulated.clear()


def surface_key(owner: str, repo: str, branch: str) -> str:
    return f"github:{owner}/{repo}:{branch}"


def store_simulated_checklist(
    *,
    owner: str,
    repo: str,
    branch: str,
    checks: dict[str, bool],
    actor: str = "system",
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Store a simulated protection checklist. Does **not** enable live_demo_ready
    unless every required check is true."""
    key = surface_key(owner, repo, branch)
    # Build a synthetic protection payload from boolean map
    payload = _checks_to_protection_payload(checks)
    evaluation = evaluate_protection_payload(payload, protected_flag=bool(checks.get("protected", True)))
    record = {
        "id": f"sim_{uuid4().hex[:16]}",
        "mode": "simulated",
        "surface_key": key,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "checks_submitted": dict(checks),
        "evaluation": evaluation,
        "satisfies_requirements": bool(evaluation["passed"]),
        "actor": actor,
        "note": note,
        "stored_at": _iso(_utcnow()),
        "warning": (
            "Simulated checklist is for tests/rehearsal only. It does not configure "
            "GitHub. Production live demos require real branch protection or an "
            "owner-approved plan upgrade path."
        ),
    }
    with _sim_lock:
        _simulated[key] = record
    return deepcopy(record)


def get_simulated(owner: str, repo: str, branch: str) -> Optional[dict[str, Any]]:
    key = surface_key(owner, repo, branch)
    with _sim_lock:
        rec = _simulated.get(key)
        return deepcopy(rec) if rec else None


def _checks_to_protection_payload(checks: dict[str, bool]) -> dict[str, Any]:
    """Map simplified boolean checklist → protection-shaped dict for evaluate()."""
    return {
        "enabled": bool(checks.get("protected", False)),
        "required_pull_request_reviews": {
            "required_approving_review_count": (
                1 if checks.get("required_approving_review_count_ge_1", False) else 0
            ),
            "dismiss_stale_reviews": bool(checks.get("dismiss_stale_reviews", False)),
        },
        "required_conversation_resolution": {
            "enabled": bool(checks.get("require_conversation_resolution", False))
        },
        "enforce_admins": {"enabled": bool(checks.get("enforce_admins", False))},
        "allow_force_pushes": {
            "enabled": not bool(checks.get("allow_force_pushes_false", False))
        },
        "allow_deletions": {
            "enabled": not bool(checks.get("allow_deletions_false", False))
        },
    }


def all_required_true_checks() -> dict[str, bool]:
    return {c["id"]: True for c in REQUIRED_CHECKS}


class BranchProtectionGateService:
    """Verify GitHub default-branch protection for live-demo readiness."""

    def __init__(
        self,
        settings: Any,
        *,
        request_fn: Optional[Callable[..., Any]] = None,
    ):
        self.settings = settings
        self._request_fn = request_fn  # injectable for tests

    def default_target(self) -> dict[str, str]:
        owner = (
            getattr(self.settings, "target_github_owner", None)
            or getattr(self.settings, "github_owner", "")
        )
        repo = (
            getattr(self.settings, "target_github_repo", None)
            or getattr(self.settings, "github_repo", "")
        )
        return {
            "owner": str(owner or "").strip(),
            "repo": str(repo or "").strip(),
            "branch": "main",
        }

    def fetch_protection(
        self, owner: str, repo: str, branch: str
    ) -> dict[str, Any]:
        """Fetch protection via adapter or raw request. Never raises for 403/404."""
        if not owner or not repo:
            return {
                "mode": "unconfigured",
                "http_status": None,
                "protection": {},
                "error": "repository_not_configured",
                "entitlement_blocked": False,
            }
        path = f"/repos/{owner}/{repo}/branches/{branch}/protection"
        if self._request_fn is not None:
            try:
                body = self._request_fn("GET", path)
                return {
                    "mode": "live",
                    "http_status": 200,
                    "protection": body if isinstance(body, dict) else {},
                    "error": None,
                }
            except Exception as error:  # noqa: BLE001
                status = getattr(getattr(error, "response", None), "status_code", None)
                text = str(error)
                return {
                    "mode": "live",
                    "http_status": status,
                    "protection": {},
                    "error": text[:500],
                    "entitlement_blocked": (
                        status == 403
                        or "upgrade to github pro" in text.lower()
                        or "pro or make this repository public" in text.lower()
                    ),
                }

        # Prefer GitHubAdapter when credentials exist
        token = getattr(self.settings, "github_token", None)
        if not token:
            return {
                "mode": "dry_run",
                "http_status": None,
                "protection": {},
                "error": "no_github_credentials",
                "entitlement_blocked": False,
            }

        try:
            from .github_adapter import GitHubAdapter

            adapter = GitHubAdapter(
                self.settings, owner=owner, repo=repo, token=token
            )
            body = adapter._request("GET", path)
            return {
                "mode": "live",
                "http_status": 200,
                "protection": body if isinstance(body, dict) else {},
                "error": None,
                "entitlement_blocked": False,
            }
        except Exception as error:  # noqa: BLE001
            status = getattr(getattr(error, "response", None), "status_code", None)
            # httpx.HTTPStatusError
            resp = getattr(error, "response", None)
            if resp is not None:
                try:
                    status = int(resp.status_code)
                except Exception:  # noqa: BLE001
                    pass
            text = str(error)
            entitlement = False
            if status == 403:
                entitlement = True
            if "upgrade to github pro" in text.lower():
                entitlement = True
            if "pro or make this repository public" in text.lower():
                entitlement = True
            if status == 404:
                # Branch exists but no protection rule
                return {
                    "mode": "live",
                    "http_status": 404,
                    "protection": {},
                    "error": "protection_not_found",
                    "entitlement_blocked": False,
                    "protected_flag": False,
                }
            return {
                "mode": "live",
                "http_status": status,
                "protection": {},
                "error": text[:500],
                "entitlement_blocked": entitlement,
            }

    def verify(
        self,
        *,
        owner: Optional[str] = None,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        accept_simulated: bool = True,
    ) -> dict[str, Any]:
        """Verify protection; fail closed on live_demo_ready."""
        target = self.default_target()
        owner = (owner or target["owner"]).strip()
        repo = (repo or target["repo"]).strip()
        branch = (branch or target["branch"]).strip() or "main"
        key = surface_key(owner, repo, branch)

        live = self.fetch_protection(owner, repo, branch)
        evaluation: Optional[dict[str, Any]] = None
        source = live["mode"]
        live_passed = False

        if live["mode"] == "live" and live.get("http_status") == 200:
            evaluation = evaluate_protection_payload(
                live.get("protection") or {},
                protected_flag=True,
            )
            live_passed = bool(evaluation["passed"])
        elif live.get("http_status") == 404:
            evaluation = evaluate_protection_payload({}, protected_flag=False)
            live_passed = False
        elif live["mode"] == "dry_run":
            evaluation = None
        else:
            # error / 403 — still evaluate empty as fail
            evaluation = evaluate_protection_payload(
                {},
                protected_flag=live.get("protected_flag", False),
            )
            live_passed = False

        simulated = get_simulated(owner, repo, branch) if accept_simulated and owner and repo else None
        sim_passed = bool(simulated and simulated.get("satisfies_requirements"))

        # Fail closed: ready only if live passes OR (explicit simulated all-pass)
        live_demo_ready = bool(live_passed or sim_passed)
        blocker: Optional[str] = None
        if not live_demo_ready:
            if live.get("error") == "repository_not_configured":
                blocker = "No repository configured; connect a project repository first."
            elif live.get("entitlement_blocked"):
                blocker = (
                    "GitHub entitlement blocked branch protection on this private "
                    "repository (typically Pro-or-public). Owner must upgrade plan, "
                    "move the sandbox to a qualifying org, or approve making it public."
                )
            elif live.get("error") == "no_github_credentials":
                blocker = (
                    "No GitHub credentials; dry-run checklist only. Configure "
                    "BOSS_GITHUB_TOKEN or store a simulated checklist for tests."
                )
            elif live.get("error"):
                blocker = f"protection fetch failed: {live.get('error')}"
            elif evaluation and not evaluation["passed"]:
                blocker = f"protection incomplete: {evaluation.get('summary')}"
            else:
                blocker = "live_demo_ready is false (fail closed)"

        return {
            "surface_key": key,
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "mode": source if live_passed or not sim_passed else (
                "simulated" if sim_passed else source
            ),
            "live": {
                "mode": live.get("mode"),
                "http_status": live.get("http_status"),
                "error": live.get("error"),
                "entitlement_blocked": bool(live.get("entitlement_blocked")),
                "evaluation": evaluation,
                "passed": live_passed,
            },
            "simulated": simulated,
            "checklist": dry_run_checklist() if evaluation is None and not simulated else None,
            "live_demo_ready": live_demo_ready,
            "blocker": blocker,
            "required_checks": [c["id"] for c in REQUIRED_CHECKS],
            "as_of": _iso(_utcnow()),
            "message": (
                "Fail closed: do not run a live PR demo until live_demo_ready is true. "
                "Simulated checklists are for tests only and do not configure GitHub."
            ),
        }
