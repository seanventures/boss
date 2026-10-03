"""What makes a deployment target a STACK target, stored in exactly one place.

A stack target deploys a whole repository from its own `boss.deploy.yaml` (see
services/stack_deploy.py). Three facts distinguish it from a per-app container target, and they
live where every other per-target fact lives — as `required_resources` entries, the way
`app_port`, `app_entry` and `repo_owner` do:

    {"kind": "deploy_kind",   "name": "stack"}
    {"kind": "manifest_path", "name": "boss.deploy.yaml"}
    {"kind": "public_url",    "name": "https://app.example.com"}

The create endpoint once dropped all three, so a target made from the console's "Add stack
target" reloaded as an ordinary target and vanished from the stack panel, while the console's
tests passed against a shape no API wrote. Every writer and reader goes through this module.
"""
from __future__ import annotations

import re
from typing import Any, Optional

DEFAULT_MANIFEST_PATH = "boss.deploy.yaml"
STACK = "stack"

#: The `required_resources` kinds a stack target carries, in the order a create writes them.
STACK_RESOURCE_KINDS = ("deploy_kind", "manifest_path", "public_url")


def resource(target: dict[str, Any], kind: str) -> Optional[str]:
    for entry in target.get("required_resources") or []:
        if isinstance(entry, dict) and entry.get("kind") == kind and entry.get("name"):
            return str(entry["name"])
    return None


def is_stack_target(target: dict[str, Any]) -> bool:
    return resource(target, "deploy_kind") == STACK


def manifest_path(target: dict[str, Any]) -> str:
    return resource(target, "manifest_path") or DEFAULT_MANIFEST_PATH


def public_url(target: dict[str, Any]) -> Optional[str]:
    return resource(target, "public_url")


def stack_resource_updates(payload: dict[str, Any]) -> dict[str, Any]:
    """The stack facts in a create/update body, validated, as `merge_target_resources` updates.

    A key that is absent stays None (left alone); an empty string clears it. Raises ValueError
    with the operator's words for a value that would only fail later, at deploy time.
    """
    updates: dict[str, Any] = {k: payload.get(k) for k in STACK_RESOURCE_KINDS}
    kind = updates["deploy_kind"]
    if kind is not None and str(kind).strip() not in ("", STACK):
        raise ValueError(f"deploy_kind must be {STACK!r} or empty, not {kind!r}")
    path = updates["manifest_path"]
    if path is not None and str(path).strip():
        p = str(path).strip()
        if p.startswith("/") or ".." in p.split("/"):
            raise ValueError(
                "manifest_path is a file inside the repository, such as boss.deploy.yaml")
    url = updates["public_url"]
    if url is not None and str(url).strip() and not re.match(r"^https?://\S+$", str(url).strip()):
        raise ValueError("public_url must start with http:// or https://")
    return updates


def rollback_option(row: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether THIS deployment offers Roll back, and the commit that would deploy.

    Decided here, once, from the target's history — not by the console from one record's
    status. Offering it on every finished record was wrong twice over: rolling back an OLDER
    healthy record planned a commit older than the one before the live release, and a
    deployment that failed at plan or preflight never touched the host, so "rolling it back"
    redeployed whatever was already running.

    `row` and `history` are `stack_deployments` rows (any order) for one target, carrying
    `id`, `status`, `commit_sha`, `apply_started_at` and `created_at`. Available only when
    `row` is the target's LATEST deployment that applied a ChangeSet (apply_started_at set),
    has finished, and an earlier healthy commit different from its own exists.
    """
    none = {"rollback_available": False, "rollback_commit": None}
    if row.get("status") not in ("healthy", "failed") or row.get("apply_started_at") is None:
        return none
    applied = [h for h in history if h.get("apply_started_at") is not None]
    latest = max(applied, key=lambda h: h["created_at"], default=None)
    if latest is None or latest.get("id") != row.get("id"):
        return none
    earlier = [h for h in history
               if h.get("status") == "healthy" and h.get("commit_sha")
               and h.get("commit_sha") != row.get("commit_sha")
               and h["created_at"] < row["created_at"]]
    if not earlier:
        return none
    return {"rollback_available": True,
            "rollback_commit": max(earlier, key=lambda h: h["created_at"])["commit_sha"]}
