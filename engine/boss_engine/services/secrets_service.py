"""Putting secrets IN — write-through upload, keypair generation, and verification.

`secret_refs` records that a secret exists; `secret_backends` fetches one at the moment of use.
Neither could put a value anywhere, so every credential still arrived by an operator editing a
file on the BOSS host. This is the missing half.

Three rules hold throughout, and they are the reason this module is small:

  * **A value passes through, it is never kept.** `put` hands material to a backend and drops
    it. BOSS keeps a reference plus a fingerprint — enough to answer "is this the same key that
    is on the host?" without holding the key.
  * **Nothing here can read a secret back to a caller.** There is no `get_secret_value()`. An
    interface that CAN return a secret eventually returns one to the wrong reader, and a
    screenshot outlives an access review.
  * **The public half of a keypair is not a secret.** It is returned, because the operator must
    install it, and making them fetch it from somewhere else is exactly how people end up
    pasting the private half instead.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from typing import Any, Optional

from ..config import Settings
from . import owned_process
from ..ports import sql
from .core import emit_event, make_id
from .secret_backends import (
    SecretUnavailable,
    backend_for,
    fingerprint_ssh_public_key,
    resolve_secret_ref,
)

# secret_refs.secret_kind is a closed set (SecretRefService.ALLOWED_SECRET_KINDS). Connector
# requirements speak a richer language ("ssh_private_key", "kubeconfig"), so the precise kind is
# kept in metadata and mapped onto the stored vocabulary rather than widening the column and
# breaking every existing reader.
_KIND_MAP = {
    "ssh_private_key": "private_key",
    "private_key": "private_key",
    "api_token": "api_token",
    "kubeconfig": "service_account",
    "service_account": "service_account",
    "access_key": "access_key",
    "client_secret": "client_secret",
}

_SAFE_NAME = re.compile(r"[^A-Z0-9]+")


def ref_key_for(scope_id: str, name: str) -> str:
    """A deterministic, human-readable key inside the backend.

    Deterministic so that re-uploading a rotated value lands on the same key rather than
    orphaning the old one, and readable so an operator browsing the store can tell what a key
    is for without consulting BOSS.
    """
    scope = _SAFE_NAME.sub("_", (scope_id or "").upper()).strip("_")
    field = _SAFE_NAME.sub("_", (name or "").upper()).strip("_")
    return f"BOSS_{scope}_{field}"


def _metadata(material: str, kind: str, extra: Optional[dict] = None) -> dict[str, Any]:
    """What BOSS is allowed to remember about a secret it just handled."""
    fingerprint = fingerprint_ssh_public_key(material) if "private_key" in kind else None
    stripped = (material or "").strip()
    return {
        "raw_secret_stored": False,
        "secret_kind_detail": kind,
        "fingerprint": fingerprint,
        # Enough to tell two credentials apart in a list; not enough to be one.
        "last4": stripped[-4:] if len(stripped) >= 8 else None,
        **(extra or {}),
    }


def put_secret(connection, settings: Settings, *, project_id: str, name: str,
               value: str, secret_kind: str = "api_token",
               backend: str = "platform_secret_store",
               scope_type: str = "project", scope_id: Optional[str] = None,
               actor: str = "operator") -> dict[str, Any]:
    """Write a value THROUGH to a backend and record the reference. Returns metadata only."""
    scope_id = scope_id or project_id
    stored_kind = _KIND_MAP.get(secret_kind, "api_token")
    key = ref_key_for(scope_id, name)

    impl = backend_for(backend, settings)
    impl.put(key, value)          # the value leaves BOSS here and is not retained

    meta = _metadata(value, secret_kind)
    ref_id = make_id("sref")
    row = connection.execute(
        sql("""INSERT INTO secret_refs
                 (id, project_id, scope_type, scope_id, ref_kind, ref_key, provider,
                  secret_kind, storage_backend, status, metadata, created_by,
                  created_at, updated_at)
               VALUES (:id,:p,:st,:si,:rk,:key,:prov,:sk,:sb,'active',CAST(:m AS JSONB),:by,
                       now(),now())
               ON CONFLICT (project_id, ref_kind, ref_key) DO UPDATE
                 SET metadata = EXCLUDED.metadata, status = 'active',
                     storage_backend = EXCLUDED.storage_backend, updated_at = now()
               RETURNING id"""),
        {"id": ref_id, "p": project_id, "st": scope_type, "si": scope_id,
         "rk": backend, "key": key, "prov": secret_kind, "sk": stored_kind,
         "sb": backend, "m": json.dumps(meta), "by": actor}).mappings().first()

    # BOSSAPP-865: each write is the reference's next version (what BOSS may remember of it).
    from .secret_access_log import record_version

    version = record_version(connection, secret_ref_id=row["id"], fingerprint=meta["fingerprint"],
                             last4=meta["last4"], actor=actor)

    # The event records THAT a secret was set, never what it was.
    emit_event(connection, project_id, "secret.written", actor, "secret_ref", row["id"],
               {"name": name, "backend": backend, "kind": secret_kind,
                "fingerprint": meta["fingerprint"], **({"version": version} if version else {})})
    return {"secret_ref_id": row["id"], "name": name, "ref_key": key,
            "backend": backend, "secret_kind": secret_kind,
            "fingerprint": meta["fingerprint"], "last4": meta["last4"],
            "raw_secret_stored": False, **({"version": version} if version else {})}


def generate_keypair(connection, settings: Settings, *, project_id: str, name: str,
                     algorithm: str = "ed25519",
                     backend: str = "platform_secret_store",
                     scope_type: str = "project", scope_id: Optional[str] = None,
                     comment: Optional[str] = None,
                     actor: str = "operator") -> dict[str, Any]:
    """Generate a keypair, store the PRIVATE half, return only the public half.

    Removes the step that has already cost this project real time: a key generated by hand in
    ~/.ssh was destroyed twice by a bug in the deploy cleanup, and nobody could say when the
    original had gone. A generated key lives in a backed-up store with a recorded fingerprint,
    so both "where is it" and "is it still the one on the host" have answers.
    """
    if algorithm not in ("ed25519", "rsa"):
        raise ValueError(f"unsupported key algorithm {algorithm!r}")

    workdir = tempfile.mkdtemp(prefix="boss-keygen-")
    path = os.path.join(workdir, "key")
    try:
        args = ["ssh-keygen", "-t", algorithm, "-f", path, "-N", "",
                "-C", comment or f"boss-deploy@{name}", "-q"]
        if algorithm == "rsa":
            args[2:2] = ["-b", "4096"]
        try:
            out = owned_process.run(args, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as exc:
            # A MISSING BINARY IS A DEPLOYMENT DEFECT, NOT A SERVER ERROR.
            #
            # python:3.11-slim ships no openssh-client, so this raised FileNotFoundError, went
            # uncaught, and reached the operator as a bare HTTP 500 on "Generate key" — a
            # message that points at nothing. The same absence would have failed every SSH
            # deploy later, further from the cause. Name the missing tool.
            raise RuntimeError(
                "ssh-keygen is not installed in this environment, so BOSS cannot mint a host "
                "key. Install openssh-client in the API image (or adopt an existing key "
                "instead of generating one)."
            ) from exc
        if out.returncode != 0:
            raise RuntimeError(f"ssh-keygen failed: {(out.stderr or '').strip()[:200]}")

        with open(path, "r", encoding="utf-8") as handle:
            private = handle.read()
        with open(path + ".pub", "r", encoding="utf-8") as handle:
            public = handle.read().strip()

        stored = put_secret(connection, settings, project_id=project_id, name=name,
                            value=private, secret_kind="ssh_private_key", backend=backend,
                            scope_type=scope_type, scope_id=scope_id, actor=actor)
    finally:
        # The private half must not outlive this function on the BOSS host.
        for p in (path, path + ".pub"):
            try:
                os.unlink(p)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass

    # `public_key` is deliberately returned; it is not a secret and the operator needs it.
    return {**stored, "public_key": public, "algorithm": algorithm,
            "install": f"echo '{public}' >> ~/.ssh/authorized_keys"}


def list_secrets(connection, project_id: str) -> list[dict[str, Any]]:
    """Metadata only. There is no variant of this that includes values."""
    rows = connection.execute(
        sql("SELECT id, scope_type, scope_id, ref_kind, ref_key, provider, secret_kind, "
            "       storage_backend, status, metadata, rotation_due_at, last_verified_at, "
            "       created_by, created_at, updated_at "
            "FROM secret_refs WHERE project_id = :p ORDER BY created_at DESC"),
        {"p": project_id}).mappings().all()

    # Which targets each secret is bound to — the question an operator actually has before
    # revoking one ("what breaks if this goes away?").
    targets = connection.execute(
        sql("SELECT id, app_name, secret_refs FROM deployment_targets WHERE project_id = :p"),
        {"p": project_id}).mappings().all()

    out = []
    for r in rows:
        meta = r["metadata"] or {}
        bound = [t["app_name"] for t in targets
                 for b in (t["secret_refs"] or [])
                 if isinstance(b, dict) and b.get("secret_ref_id") == r["id"]]
        out.append({
            "secret_ref_id": r["id"], "name": r["ref_key"], "backend": r["storage_backend"],
            "scope_type": r["scope_type"], "scope_id": r["scope_id"],
            "secret_kind": meta.get("secret_kind_detail") or r["secret_kind"],
            "status": r["status"], "fingerprint": meta.get("fingerprint"),
            "last4": meta.get("last4"), "bound_to": bound,
            "rotation_due_at": r["rotation_due_at"].isoformat() if r["rotation_due_at"] else None,
            "last_verified_at": (r["last_verified_at"].isoformat()
                                 if r["last_verified_at"] else None),
            "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out


def verify_secret(connection, settings: Settings, secret_ref_id: str, *,
                  actor: str = "operator") -> dict[str, Any]:
    """Can this reference still be resolved, and is it the same material as when stored?

    Answers the question a list of secrets cannot: a row saying `active` proves only that
    nobody marked it otherwise. Resolution is the proof.
    """
    try:
        material = resolve_secret_ref(connection, secret_ref_id, settings)
    except SecretUnavailable as exc:
        connection.execute(
            sql("UPDATE secret_refs SET status = 'disabled', updated_at = now() "
                "WHERE id = :i AND status = 'active'"), {"i": secret_ref_id})
        return {"secret_ref_id": secret_ref_id, "resolvable": False, "error": str(exc)}

    row = connection.execute(sql("SELECT project_id, metadata FROM secret_refs WHERE id = :i"),
                             {"i": secret_ref_id}).mappings().first()
    meta = dict((row or {}).get("metadata") or {})
    current = fingerprint_ssh_public_key(material)
    unchanged = (current == meta.get("fingerprint")) if (current and meta.get("fingerprint")) \
        else None

    connection.execute(
        sql("UPDATE secret_refs SET last_verified_at = now(), updated_at = now() WHERE id = :i"),
        {"i": secret_ref_id})
    if row:
        emit_event(connection, row["project_id"], "secret.verified", actor, "secret_ref",
                   secret_ref_id, {"resolvable": True, "fingerprint_unchanged": unchanged})
    return {"secret_ref_id": secret_ref_id, "resolvable": True,
            "fingerprint": current, "fingerprint_unchanged": unchanged}


def bind_secret_to_target(connection, *, target_id: str, name: str, secret_ref_id: str,
                          project_id: str, actor: str = "operator") -> dict[str, Any]:
    """Point a target's named requirement at a stored reference.

    Replaces any existing binding for that name rather than appending: two bindings for one
    requirement would make "which credential does this target use" ambiguous, and the resolver
    would silently answer with whichever came first.
    """
    row = connection.execute(
        sql("SELECT secret_refs FROM deployment_targets WHERE id = :i AND project_id = :p"),
        {"i": target_id, "p": project_id}).mappings().first()
    if not row:
        raise ValueError(f"no deployment target {target_id}")

    owned_ref = connection.execute(
        sql("SELECT 1 FROM secret_refs WHERE id = :id AND project_id = :project_id"),
        {"id": secret_ref_id, "project_id": project_id},
    ).first()
    if not owned_ref:
        raise ValueError("Secret ref is not in this project.")

    bindings = [b for b in (row["secret_refs"] or [])
                if isinstance(b, dict) and b.get("name") != name]
    bindings.append({"name": name, "secret_ref_id": secret_ref_id})
    connection.execute(
        sql("UPDATE deployment_targets SET secret_refs = CAST(:s AS JSONB), updated_at = now() "
            "WHERE id = :i"), {"s": json.dumps(bindings), "i": target_id})
    emit_event(connection, project_id, "secret.bound", actor, "deployment_target", target_id,
               {"name": name, "secret_ref_id": secret_ref_id})
    return {"target_id": target_id, "bindings": bindings}
