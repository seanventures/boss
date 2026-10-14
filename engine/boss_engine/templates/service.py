"""The template catalogue: built-ins, imports, review, trial and the gallery (F2 §2.2, §2.5, §2.6).

Every function takes an open connection (`c`) and does its own SQL; the API maps the errors.

Built-ins live in the repo (`templates/apps/<id>/`, D1). `sync_builtins` validates each one and
upserts it for an org as `source=builtin, status=active` with its files, so apply, the gate and
the gallery read every template the same way: from `app_templates` + `app_template_files`,
pinned by digest. A built-in that fails validation is not offered (CI's coverage test keeps that
from shipping).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import text

from ..services.core import make_id
from .manifest import TemplateManifest, kind_of, validate_manifest
from .validator import BRIEF_FILE, ValidationReport, tree_from_dir, validate_tree

log = logging.getLogger("boss.templates")

TEMPLATES_DIR_ENV = "BOSS_TEMPLATES_DIR"


class TemplateError(Exception):
    """A refused operation. `status` is the HTTP status the API answers; `detail` is safe to
    show (never a secret, never template content beyond names)."""

    def __init__(self, status: int, error: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.body = {"error": error, "message": message, **extra}


# ---- built-ins -----------------------------------------------------------------------------------

def builtin_dir() -> Path:
    configured = os.environ.get(TEMPLATES_DIR_ENV)
    if configured:
        return Path(configured)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "templates" / "apps"
        if candidate.is_dir():
            return candidate
    return here.parents[3] / "templates" / "apps"


@lru_cache(maxsize=4)
def _load_builtins(root: str) -> tuple[tuple[str, ValidationReport, dict], ...]:
    out = []
    base = Path(root)
    if not base.is_dir():
        return ()
    for d in sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")):
        tree = tree_from_dir(d)
        report = validate_tree(tree, source="builtin")
        if not report.ok:
            log.error("built-in template %s is invalid and is not offered: %s", d.name, report.reasons)
        out.append((d.name, report, tree.files()))
    return tuple(out)


def builtins() -> list[tuple[str, ValidationReport, dict]]:
    """(directory name, report, files) for every built-in, valid or not (the CI test reads all)."""
    return list(_load_builtins(str(builtin_dir())))


def builtin_ids() -> set[str]:
    return {r.manifest.id for _, r, _ in builtins() if r.manifest is not None}


def _put_files(c, template_id: str, files: dict[str, bytes]) -> None:
    c.execute(text("DELETE FROM app_template_files WHERE template_id = :t"), {"t": template_id})
    for path, content in sorted(files.items()):
        c.execute(text("INSERT INTO app_template_files (template_id, path, content) VALUES (:t, :p, :b)"),
                  {"t": template_id, "p": path, "b": content})


def sync_builtins(c, org_id: str) -> int:
    """Make every valid built-in available to `org_id` (idempotent; a hidden one stays hidden).
    Returns how many rows were written or refreshed."""
    written = 0
    for _name, report, files in builtins():
        m = report.manifest
        if not report.ok or m is None:
            continue
        row = c.execute(text(
            "SELECT id, digest, status FROM app_templates WHERE org_id = :o AND template_key = :k "
            "AND version = :v"), {"o": org_id, "k": m.id, "v": m.version}).mappings().first()
        if row is not None and row["digest"] == report.digest:
            continue
        manifest_json = json.dumps(m.model_dump(mode="json", by_alias=True))
        if row is None:
            tid = make_id("tpl")
            c.execute(text(
                "INSERT INTO app_templates (id, org_id, template_key, version, source, digest, manifest, "
                "status, validation, approved_at) VALUES (:id, :o, :k, :v, 'builtin', :d, "
                "CAST(:m AS JSONB), 'active', CAST(:val AS JSONB), now()) "
                "ON CONFLICT (org_id, template_key, version) DO NOTHING"),
                {"id": tid, "o": org_id, "k": m.id, "v": m.version, "d": report.digest,
                 "m": manifest_json, "val": json.dumps(report.as_dict())})
            got = c.execute(text("SELECT id FROM app_templates WHERE org_id = :o AND template_key = :k "
                                 "AND version = :v"), {"o": org_id, "k": m.id, "v": m.version}).scalar()
            if got != tid:
                continue           # a concurrent sync won; it wrote the files
        else:
            tid = row["id"]
            c.execute(text("UPDATE app_templates SET digest = :d, manifest = CAST(:m AS JSONB), "
                           "validation = CAST(:val AS JSONB) WHERE id = :id"),
                      {"id": tid, "d": report.digest, "m": manifest_json,
                       "val": json.dumps(report.as_dict())})
        _put_files(c, tid, files)
        written += 1
    return written


# ---- reading -------------------------------------------------------------------------------------

_COLS = ("id, org_id, template_key, version, source, git_url, commit_sha, digest, manifest, status, "
         "validation, trial, imported_by, approved_by, created_at, approved_at")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _card(row: Any) -> dict:
    m = row["manifest"] or {}
    trial = row["trial"] or {}
    tested = sorted(s for s, r in (trial.get("stacks") or {}).items() if (r or {}).get("ok"))
    return {
        "id": row["id"], "template_key": row["template_key"], "version": row["version"],
        "source": row["source"], "status": row["status"], "name": m.get("name"),
        "summary": m.get("summary"), "stacks": sorted((m.get("stacks") or {}).keys()),
        "labels": list(m.get("labels") or []), "adds": list(m.get("adds") or []),
        "kind": kind_of(m),
        "egress": list(m.get("egress") or []),
        "secrets": [{"name": s.get("name"), "purpose": s.get("purpose"),
                     "test_default": s.get("test_default", "none")} for s in (m.get("secrets") or [])],
        "tested": tested, "digest": row["digest"],
        "git_url": row["git_url"], "commit_sha": row["commit_sha"],
        "imported_by": row["imported_by"], "approved_by": row["approved_by"],
        "created_at": _iso(row["created_at"]), "approved_at": _iso(row["approved_at"]),
    }


def ensure_builtins(c, org_id: str) -> None:
    """The built-ins for an org that has NEVER had them: an org created after the API booted.

    Refreshing a CHANGED built-in is the API's boot step (main._sync_builtin_templates), so a
    read stays a read: listing or describing templates writes nothing once an org has them.
    """
    have = c.execute(text("SELECT 1 FROM app_templates WHERE org_id = :o AND source = 'builtin' "
                          "LIMIT 1"), {"o": org_id}).first()
    if have is None:
        sync_builtins(c, org_id)


def list_templates(c, org_id: str, *, stack: Optional[str] = None, status: Optional[str] = None,
                   label: Optional[str] = None, include_hidden: bool = False,
                   kind: Optional[str] = None) -> list[dict]:
    """The catalogue. `kind` narrows it to connectors, ideas or training. Training (insecure by
    design) is offered only to a caller that asks for it: by kind, by its label, or hidden ones."""
    ensure_builtins(c, org_id)
    rows = c.execute(text(f"SELECT {_COLS} FROM app_templates WHERE org_id = :o "
                          "ORDER BY template_key, created_at DESC"), {"o": org_id}).mappings().all()
    out = []
    for r in rows:
        card = _card(r)
        if status and card["status"] != status:
            continue
        if not status and not include_hidden and card["status"] in ("hidden", "rejected"):
            continue
        if "composition" in card["labels"]:
            continue           # a build's merged connectors (templates.compose), never an item to pick
        if stack and stack not in card["stacks"]:
            continue
        if label and label not in card["labels"]:
            continue
        if kind and card["kind"] != kind:
            continue
        if card["kind"] == "training" and not (kind == "training" or label or include_hidden):
            continue
        out.append(card)
    return out


def get_row(c, org_id: str, template_ref: str, *, for_update: bool = False) -> dict:
    """A template by its row id, or by its key (the newest active version)."""
    ensure_builtins(c, org_id)
    lock = " FOR UPDATE" if for_update else ""
    row = c.execute(text(f"SELECT {_COLS} FROM app_templates WHERE org_id = :o AND id = :r{lock}"),
                    {"o": org_id, "r": template_ref}).mappings().first()
    if row is None:
        row = c.execute(text(
            f"SELECT {_COLS} FROM app_templates WHERE org_id = :o AND template_key = :r "
            f"AND status = 'active' ORDER BY approved_at DESC NULLS LAST, created_at DESC LIMIT 1{lock}"),
            {"o": org_id, "r": template_ref}).mappings().first()
    if row is None:
        raise TemplateError(404, "template_not_found", "no such template in this org")
    return dict(row)


def files_of(c, template_id: str, *, prefix: str = "") -> dict[str, bytes]:
    rows = c.execute(text("SELECT path, content FROM app_template_files WHERE template_id = :t "
                          "AND path LIKE :p ORDER BY path"),
                     {"t": template_id, "p": prefix.replace("%", r"\%").replace("_", r"\_") + "%"}).all()
    return {r[0]: bytes(r[1]) for r in rows}


def _text_file(files: dict[str, bytes], name: str, limit: int = 64_000) -> Optional[str]:
    raw = files.get(name)
    return None if raw is None else raw[:limit].decode("utf-8", errors="replace")


def describe(c, org_id: str, template_ref: str) -> dict:
    row = get_row(c, org_id, template_ref)
    m = row["manifest"] or {}
    files = files_of(c, row["id"])
    versions = c.execute(text(
        "SELECT id, version, status, created_at FROM app_templates WHERE org_id = :o AND template_key = :k "
        "ORDER BY created_at DESC"), {"o": org_id, "k": row["template_key"]}).mappings().all()
    return {
        **_card(row), "manifest": m,
        # Rendered as TEXT by the console, never as HTML or instructions (§2.9 S2.3).
        "brief": _text_file(files, BRIEF_FILE),
        "guide": _text_file(files, m.get("guide") or "guide.md"),
        "security_note": _text_file(files, m.get("security_note") or "SECURITY.md"),
        "files": [{"path": p, "size": len(b)} for p, b in sorted(files.items())],
        "validation": row["validation"] or {}, "trial": row["trial"],
        "versions": [{"id": v["id"], "version": v["version"], "status": v["status"],
                      "created_at": _iso(v["created_at"])} for v in versions],
    }


# ---- import, review --------------------------------------------------------------------------------

def boss_hosts() -> list[str]:
    """This install's own hosts: a template may not ask its app to talk to BOSS itself."""
    from urllib.parse import urlsplit

    hosts = []
    for key in ("BOSS_PUBLIC_URL", "BOSS_CONSOLE_URL", "BOSS_API_PUBLIC_URL", "NEXT_PUBLIC_BOSS_API_URL"):
        value = os.environ.get(key) or ""
        host = urlsplit(value).hostname if "://" in value else None
        if host:
            hosts.append(host)
    return hosts


def record_import(c, org_id: str, *, actor_id: Optional[str], git_url: str, commit_sha: Optional[str],
                  report: ValidationReport, files: dict[str, bytes]) -> dict:
    """Store an import's outcome: `pending_review` with its files when valid, else `rejected`
    with the reasons (and no files). Re-importing a version that was rejected replaces it; a
    version already pending or active is a 409 (a new version is a new import)."""
    m = report.manifest
    key = m.id if m is not None else f"invalid-{(commit_sha or make_id('x'))[:12]}"
    version = m.version if m is not None else "0.0.0"
    existing = c.execute(text("SELECT id, status FROM app_templates WHERE org_id = :o AND template_key = :k "
                              "AND version = :v FOR UPDATE"),
                         {"o": org_id, "k": key, "v": version}).mappings().first()
    if existing is not None:
        if existing["status"] != "rejected":
            raise TemplateError(409, "version_exists",
                                f"{key} {version} is already {existing['status']}; "
                                "bump the version to import a new one", id=existing["id"])
        c.execute(text("DELETE FROM app_templates WHERE id = :id"), {"id": existing["id"]})
    status = "pending_review" if report.ok else "rejected"
    tid = make_id("tpl")
    c.execute(text(
        "INSERT INTO app_templates (id, org_id, template_key, version, source, git_url, commit_sha, digest, "
        "manifest, status, validation, imported_by) VALUES (:id, :o, :k, :v, 'git', :u, :sha, :d, "
        "CAST(:m AS JSONB), :s, CAST(:val AS JSONB), :by)"),
        {"id": tid, "o": org_id, "k": key, "v": version, "u": git_url, "sha": commit_sha,
         "d": report.digest, "s": status, "by": actor_id,
         "m": json.dumps(m.model_dump(mode="json", by_alias=True) if m is not None else {}),
         "val": json.dumps(report.as_dict())})
    if report.ok:
        _put_files(c, tid, files)
    return {"id": tid, "template_key": key, "version": version, "status": status,
            "commit_sha": commit_sha, "digest": report.digest, "report": report.as_dict()}


def import_from_git(c_factory: Callable[[], Any], org_id: str, *, actor_id: Optional[str], git_url: str,
                    ref: Optional[str], may_label_insecure: bool, ack_insecure: bool,
                    fetcher: Optional[Callable[..., Any]] = None, allow_local: bool = False,
                    github_access: Optional[Callable[..., Any]] = None) -> dict:
    """Fetch (outside any transaction: it may take a while), validate, record. Nothing from the
    template is executed. `c_factory()` opens the transaction the record is written in."""
    from . import importer

    fetch = fetcher or importer.fetch
    kwargs: dict[str, Any] = {"allow_local": allow_local}
    if github_access is not None:
        kwargs["github_access"] = github_access
    try:
        fetched = fetch(git_url, ref, **kwargs)
    except importer.ImportRefused as exc:
        raise TemplateError(422, "import_refused", exc.reason) from None
    report = validate_tree(fetched.tree, source="git", builtin_ids=builtin_ids(),
                           boss_hosts=boss_hosts(), may_label_insecure=may_label_insecure,
                           ack_insecure=ack_insecure)
    if report.manifest is None:
        # No readable manifest: not a template at all. Nothing is stored (there is no name or
        # version to file it under); the reasons go back to the person who asked.
        raise TemplateError(422, "invalid_template",
                            "this repository is not a valid BOSS template: " + "; ".join(report.reasons[:5]),
                            report=report.as_dict())
    with c_factory() as c:
        return record_import(c, org_id, actor_id=actor_id, git_url=fetched.git_url,
                             commit_sha=fetched.commit_sha, report=report, files=fetched.tree.files())


def approve(c, org_id: str, template_id: str, *, actor_id: Optional[str]) -> dict:
    """D3: an Owner/Admin (template:approve, checked by the route) who is NOT the importer.
    Pins (git_url, commit_sha, digest): the row is already pinned; approval makes it usable."""
    row = get_row(c, org_id, template_id, for_update=True)
    if row["source"] != "git":
        raise TemplateError(409, "not_an_import", "built-in templates need no approval")
    if row["status"] != "pending_review":
        raise TemplateError(409, "not_pending", f"this template is {row['status']}, not pending review")
    if not actor_id:
        raise TemplateError(401, "unauthenticated", "approving needs a signed-in person")
    if row["imported_by"] and row["imported_by"] == actor_id:
        raise TemplateError(403, "seat_separation",
                            "the person who imported a template cannot approve it; another Owner or Admin approves it")
    if not (row["validation"] or {}).get("ok"):
        raise TemplateError(409, "invalid", "this import did not pass validation")
    c.execute(text("UPDATE app_templates SET status = 'active', approved_by = :a, approved_at = now() "
                   "WHERE id = :id"), {"a": actor_id, "id": row["id"]})
    return {"id": row["id"], "status": "active", "approved_by": actor_id,
            "pinned": {"git_url": row["git_url"], "commit_sha": row["commit_sha"], "digest": row["digest"]}}


def reject(c, org_id: str, template_id: str, *, actor_id: Optional[str], reason: str = "") -> dict:
    row = get_row(c, org_id, template_id, for_update=True)
    if row["source"] != "git":
        raise TemplateError(409, "not_an_import", "a built-in template cannot be rejected; hide it")
    if row["status"] not in ("pending_review", "active"):
        raise TemplateError(409, "not_pending", f"this template is {row['status']}")
    validation = dict(row["validation"] or {})
    validation["rejected"] = {"by": actor_id, "reason": (reason or "")[:500],
                              "at": datetime.now(timezone.utc).isoformat()}
    c.execute(text("UPDATE app_templates SET status = 'rejected', validation = CAST(:v AS JSONB) "
                   "WHERE id = :id"), {"v": json.dumps(validation), "id": row["id"]})
    c.execute(text("DELETE FROM app_template_files WHERE template_id = :id"), {"id": row["id"]})
    return {"id": row["id"], "status": "rejected"}


def delete(c, org_id: str, template_id: str) -> dict:
    """An import is deleted (unless an app applied it: then hidden); a built-in is only hidden."""
    row = get_row(c, org_id, template_id, for_update=True)
    if row["source"] == "builtin":
        raise TemplateError(403, "builtin", "built-in templates cannot be deleted, only hidden",
                            hint="DELETE /v1/templates/{id}?hide=true hides it")
    used = c.execute(text("SELECT 1 FROM app_template_applications WHERE template_id = :t LIMIT 1"),
                     {"t": row["id"]}).first()
    if used:
        c.execute(text("UPDATE app_templates SET status = 'hidden' WHERE id = :id"), {"id": row["id"]})
        return {"id": row["id"], "status": "hidden", "note": "an app applied it, so it is hidden, not deleted"}
    c.execute(text("DELETE FROM app_templates WHERE id = :id"), {"id": row["id"]})
    return {"id": row["id"], "status": "deleted"}


def hide(c, org_id: str, template_id: str) -> dict:
    row = get_row(c, org_id, template_id, for_update=True)
    c.execute(text("UPDATE app_templates SET status = 'hidden' WHERE id = :id"), {"id": row["id"]})
    return {"id": row["id"], "status": "hidden"}


def record_trial(c, template_id: str, result: dict) -> None:
    c.execute(text("UPDATE app_templates SET trial = CAST(:t AS JSONB) WHERE id = :id"),
              {"t": json.dumps(result), "id": template_id})


def manifest_of(row: dict) -> TemplateManifest:
    return validate_manifest(dict(row["manifest"] or {}))


def stacks_supported(rows: Iterable[dict]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        out |= set((r.get("manifest") or {}).get("stacks") or {})
    return out
