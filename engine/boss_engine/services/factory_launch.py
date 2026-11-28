"""Easy Mode: one launch creates a project and starts its build (BOSSAPP-1274).

Empty prompt picks a concrete spec (the first template file by id, else a built-in spec)
and then runs the same path as a prompt the person wrote. A non-empty prompt is the spec.
The path creates the project, assigns the resources the spec names, writes the workflows
the spec names, reserves a budget, writes one receipt, and only then starts the build.

THE SPEC BEFORE THE BUILD (operator, 2026-11-19): a normal launch starts the Easy Mode MVP
workflow — a compact agent panel writes minutes and a lean PRD, the person's start approves it,
and the build runs from it, then the preview and a staging deploy (`spec_first_run`). Its run id
is that workflow run's. A bug keeps the small path: the code task starts directly.
The blurb and the prompt are checked before that reserve. A card number or a national id
blocks, so nothing is reserved and the build does not start. An email or a phone number
asks, and nothing is reserved until that ask is allowed. A result without a project id
and a run id is a failure.

Consent (the build start answers ConsentRequired): only the start is undone (a savepoint takes
the reservation back). The project, its role agents with their Passports and its workflows are
kept with one in-progress receipt, and the 409 names the project and the dedupe key. The caller
commits. The same launch sent again after the person grants (the console re-sends it unchanged)
finds that receipt and starts the build in the same project; it never makes a second one.

Attachments (BOSSAPP-1275) are stored for the org and folded in by name and text extract.
A Drive source that is not already connected is refused. Nothing here calls Drive or GitHub.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml
from sqlalchemy import text

from ..identity.consent import ConsentRequired
from ..multirepo.common import MultiRepoError, norm_repo
from ..multirepo.registry import NOT_CONNECTED
from . import stacks as _stacks
from .core import make_id
from .personal_data_gate import before_builder_reserve
from .projects import create as create_project
from .receipts import ReceiptService
from .workflows import (
    PRODUCT_NAMESPACE,
    WorkflowError,
    create_definition,
    publish_version,
)

#: `engine/boss_engine/data/factory_templates/*.yaml`. Missing means there is no catalogue yet.
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "data" / "factory_templates"

#: Small on purpose: a launch attachment is a brief, not a dump.
MAX_ATTACHMENT_BYTES = 256 * 1024
TEXT_EXTRACT_LIMIT = 8000

_TEMPLATE_FIELDS = ("id", "title", "blurb", "category", "prompt", "stack", "toolchain",
                    "resources", "workflows")

#: WHAT A TEMPLATE BUILDS WITH (stranger test 2026-11-18: the gallery offered Android and Godot
#: templates no install can build). Python and Node are in every build image; Go and Java are
#: opt-in toolchains (services/toolchains.KNOWN, `boss toolchains enable go`). A catalogue
#: template names one of these; anything else is not loaded. Read from the one registry of what
#: BOSS builds (services/stacks.LANGUAGES), never a second copy.
TEMPLATE_TOOLCHAINS = tuple(_stacks.LANGUAGES)
BUILTIN_TOOLCHAINS = tuple(k for k, lang in _stacks.LANGUAGES.items() if lang.toolchain is None)

#: Platforms no BOSS image carries (no Android SDK or emulator, no Xcode, no game-engine binary).
#: A template whose category, stack or toolchain names one is not loaded, and a pasted spec that
#: does is refused in words. engine/tests/test_factory_template_catalog.py also holds every
#: shipped template's title, blurb and prompt to this same pattern.
UNBUILDABLE_PLATFORM = re.compile(
    r"\b(android|jetpack|kotlin|ios|swift(?: ?ui)?|xcode|emulator|godot|unity|unreal|"
    r"react native|flutter|expo|apk|play store|app store)\b", re.I)

BUILTIN_SPEC: dict[str, Any] = {
    "id": "lucky-notes",
    "title": "A small notes app",
    "blurb": "A list of notes and an editor, on one page.",
    "category": "app",
    "prompt": "Build a small notes app: a list of notes and an editor. One page, no accounts.",
    "stack": "vite-react",
    "toolchain": "node",
    "resources": ["postgres"],
    "workflows": [
        {"name": "Build the notes app", "steps": ["Draft the page", "Save a note"]},
    ],
}


class FactoryLaunchError(Exception):
    """A refusal with a status, a machine code and a sentence a person can act on."""

    def __init__(self, status: int, code: str, message: str,
                 fix_href: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fix_href = fix_href

    def body(self) -> dict[str, str]:
        out = {"error": self.code, "message": self.message}
        if self.fix_href:
            out["fix_href"] = self.fix_href
        return out


def list_templates(
    root: Optional[Path] = None, have: Optional[set[str]] = None,
) -> dict[str, Any]:
    """The catalogue on disk. A file that is not the schema is skipped and counted, not fatal.

    Every template says whether THIS install can build it (`available`) and, when it cannot,
    what it needs (`needs`: the toolchain, its name for a person, and the command that turns it
    on). `have` is the opt-in toolchains this image carries (default: the toolchain manifest)."""
    directory = Path(root) if root is not None else TEMPLATE_DIR
    if not directory.is_dir():
        return {"templates": [], "skipped": 0}
    problem = None
    if have is None:
        have, problem = _installed()
    found: list[dict[str, Any]] = []
    skipped = 0
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        try:
            item = _template(yaml.safe_load(path.read_text(encoding="utf-8")), catalogue=True)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError):
            skipped += 1
            continue
        if item["id"] in seen:
            skipped += 1
            continue
        seen.add(item["id"])
        found.append({**item, **availability(item, have, problem)})
    found.sort(key=lambda item: item["id"])
    return {"templates": found, "skipped": skipped}


def _installed() -> tuple[set[str], Optional[str]]:
    """The opt-in toolchains this image carries, and why that cannot be known (the manifest is
    missing, unreadable or not JSON — toolchains.manifest_problem), else None."""
    from . import toolchains

    problem = toolchains.manifest_problem()
    return (set() if problem else set(toolchains.installed())), problem


def availability(
    spec: dict[str, Any], have: set[str], problem: Optional[str] = None,
) -> dict[str, Any]:
    """{"available": bool, "needs": None | {...}} for a spec on an install carrying `have`.
    `problem`: the manifest could not be read, so what is turned on is not known."""
    name = str(spec.get("toolchain") or "")
    lang = _stacks.LANGUAGES.get(name)
    needed = lang.toolchain if lang is not None else name
    if not name or needed is None or needed in have:
        return {"available": True, "needs": None}
    from . import toolchains

    label = toolchains.LABELS.get(needed, needed)
    title = spec.get("title") or "This template"
    if problem:
        # NEVER "IT IS OFF" ON A GUESS: an unreadable manifest says nothing about what is on.
        return {"available": False, "needs": {
            "toolchain": needed,
            "label": "Can't check toolchains",
            "enable": "boss update",
            "message": (f"{title} is built in {label}, and BOSS cannot read its toolchain list "
                        f"({problem}), so it cannot tell whether {label} is turned on. Rebuild "
                        "the images with `boss update` (and run `boss doctor` if it persists), "
                        "then pick it again."),
        }}
    command = toolchains.ENABLE_HINT.format(name=needed)
    return {"available": False, "needs": {
        "toolchain": needed,
        "label": f"Needs {label}",
        "enable": command,
        "message": (f"{title} is built in {label}, and {label} is "
                    f"not turned on for this BOSS. Turn it on with `{command}` on the BOSS host, "
                    "then pick it again."),
    }}


def _require_buildable(spec: dict[str, Any]) -> None:
    """Refuse, in words and before anything is made or reserved, a spec whose toolchain this
    install does not carry."""
    needs = availability(spec, *_installed())["needs"]
    if needs:
        raise FactoryLaunchError(409, "toolchain_not_enabled", "Not started: " + needs["message"])


def _plain(spec: dict[str, Any]) -> dict[str, Any]:
    """A catalogue item as a launch spec: what this install has is not part of the spec."""
    return {k: v for k, v in spec.items() if k not in ("available", "needs")}


def store_attachments(
    connection: Any,
    *,
    org_id: str,
    uploads: list[tuple[str, str, str, bytes]],
    drive_source_id: str = "",
    actor: Optional[str] = None,
) -> dict[str, Any]:
    """Keep specs, files and images for this org. A Drive source is accepted only when that
    source is already connected here — the bytes are never fetched."""
    prepared: list[dict[str, Any]] = []
    for kind, name, media, data in uploads:
        filename = _filename(name)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise FactoryLaunchError(
                413, "file_too_large",
                f"'{filename}' is {len(data)} bytes. The most an attachment may be is "
                f"{MAX_ATTACHMENT_BYTES} bytes.")
        if not data:
            raise FactoryLaunchError(422, "empty_attachment", f"'{filename}' is empty.")
        media_is_image = str(media).lower().startswith("image/")
        stored_kind = "image" if kind == "image" or media_is_image else kind
        if stored_kind not in ("spec", "file", "image"):
            stored_kind = "file"
        prepared.append({
            "kind": stored_kind,
            "name": filename,
            "media_type": (media or "")[:200],
            "byte_size": len(data),
            "text_extract": _extract(stored_kind, data),
            "drive_source_id": None,
        })
    source = str(drive_source_id or "").strip()
    if source:
        connected = _connected_drive(connection, org_id, source)
        if connected is None:
            raise FactoryLaunchError(
                409, "drive_not_connected",
                "That Drive source is not connected. No Google account is connected for this "
                "organization. BOSS did not fetch it.")
        prepared.append({
            "kind": "drive",
            "name": connected,
            "media_type": "",
            "byte_size": 0,
            "text_extract": "",
            "drive_source_id": source,
        })
    if not prepared:
        raise FactoryLaunchError(
            422, "attachment_required",
            "Attach a spec, a file, an image, or a Drive source that is already connected.")
    rows = []
    for item in prepared:
        attachment_id = make_id("fatt")
        connection.execute(text(
            """
            INSERT INTO factory_launch_attachments (
                id, org_id, kind, name, media_type, byte_size, text_extract, drive_source_id,
                uploaded_by
            ) VALUES (
                :id, :org, :kind, :name, :media, :size, :extract, :drive, :uploaded_by
            )
            """),
            {"id": attachment_id, "org": org_id, "kind": item["kind"], "name": item["name"],
             "media": item["media_type"], "size": item["byte_size"],
             "extract": item["text_extract"], "drive": item["drive_source_id"],
             "uploaded_by": actor})
        rows.append({"id": attachment_id, "name": item["name"], "kind": item["kind"],
                     "byte_size": item["byte_size"], "text_extract": item["text_extract"]})
    return {"attachments": rows}


def launch(
    connection: Any,
    settings: Any,
    *,
    org_id: str,
    actor: str,
    prompt: str = "",
    mode: str = "",
    template_id: Optional[str] = None,
    attachment_ids: Optional[list[str]] = None,
    github_repo: Optional[str] = None,
    template_dir: Optional[Path] = None,
    builder: Optional[Callable[..., dict[str, Any]]] = None,
    jobs: Any = None,
    budget: Any = None,
    auth: Any = None,
    allowed: bool = False,
    review_plan: bool = False,
    target_project_id: Optional[str] = None,
) -> dict[str, Any]:
    """Create the project and start the build. The builder runs only after the budget is reserved
    and only a result that names a project and a run is returned. Personal data in the blurb or
    the prompt is decided before that reserve."""
    ids = [str(item).strip() for item in (attachment_ids or []) if str(item).strip()]
    repo = str(github_repo or "").strip()
    asked = str(mode or "").strip()
    # WHOSE launch it is: two people pressing the same template never share a kept or a reused
    # project (nor a consent the other one owes).
    who = str(getattr(getattr(auth, "principal", None), "id", "") or actor or "")
    target = None
    if target_project_id is not None:
        if repo:
            raise FactoryLaunchError(422, "repository_override", "Choose the project's repository in Setup.")
        target = connection.execute(text(
            "SELECT p.workspace_id, b.full_name FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "LEFT JOIN LATERAL (SELECT full_name FROM github_repository_bindings WHERE project_id = p.id "
            "AND status = 'active' ORDER BY (role = 'primary') DESC, created_at LIMIT 1) b ON true "
            "WHERE p.id = :p AND w.org_id = :o AND p.archived_at IS NULL"),
            {"p": target_project_id, "o": org_id}).mappings().first()
        if target is None:
            raise FactoryLaunchError(404, "missing_project", "Choose an active project in this organization.")
        if not target["full_name"]:
            raise FactoryLaunchError(409, "no_repository", "Connect a repository in this project's Setup before starting the build.")
    dedupe_key = _dedupe_key(org_id, prompt, asked, template_id, ids, repo, who,
                             target_project_id=target_project_id,
                             review_plan=bool(review_plan))
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": dedupe_key})
    previous = _reuse(connection, org_id, dedupe_key)
    if previous is not None:
        return previous

    kept = _kept(connection, org_id, dedupe_key)
    # THE SAME LAUNCH AGAIN, AFTER ITS BUILD ENDED (a failed run, a finished one): a new run in
    # the project that launch made — never the dead run handed back, never a second project
    # (the slug is the launch's own). When that project is gone it is made again, and the
    # launch's one receipt is still the one that remembers the new run.
    again = None if kept is not None else _relaunch(connection, org_id, dedupe_key)
    if kept is not None:
        # THE SAME LAUNCH AFTER ITS CONSENT (or after a cancel): the project, its agents and its
        # workflows were kept when the build asked for consent. Start the build there; never a
        # second project.
        spec = dict(kept["spec"])
        project_id = str(kept["project_id"])
        workspace_id = str(kept["workspace_id"])
        resources = list(kept.get("resources") or [])
        workflows = list(kept.get("workflows") or [])
        receipt_id: Optional[str] = str(kept["receipt_id"])
    else:
        spec, origin = resolve_spec(prompt, template_id, template_dir)
        if target is not None and origin != "template":
            # INTO A PROJECT BUILDS A CATALOGUE TEMPLATE, nothing else: a pasted spec (its own
            # repository, resources and workflows) or a template the catalogue does not have
            # never lands in a project that already exists.
            raise FactoryLaunchError(422, "template_required",
                                     "Choose a template from the catalogue to build into this project.")
        attachments = _load_attachments(connection, org_id, ids)
        spec = _fold_attachments(spec, attachments)
        if repo:
            _require_github_repo(connection, org_id, repo)
            spec["github_repo"] = repo
        spec["mode"] = asked or origin
        # THE SPEC BEFORE THE BUILD (operator, 2026-11-19): a normal launch convenes the panel
        # (Easy Mode MVP workflow); a bug or a small fix keeps the small path, as New work does.
        spec["spec_first"] = spec_first(spec["mode"])
        spec["spec_approval"] = (SPEC_BY_PERSON if review_plan else SPEC_BY_START)
        # INTO AN EXISTING PROJECT: only this server sets the marker, never a template or a prompt.
        spec.pop("existing_project", None)
        if target is not None:
            from .local_repo import is_local_owner

            full_name = str(target["full_name"])
            spec.pop("github_repo", None)
            spec.pop("local_repo", None)
            spec["local_repo" if is_local_owner(full_name.split("/", 1)[0]) else "github_repo"] = full_name
            spec["existing_project"] = True

    if again is not None and again.get("project_id"):
        project_id = str(again["project_id"])
        workspace_id = str(again["workspace_id"])
        resources = list(again.get("resources") or [])
        workflows = list(again.get("workflows") or [])
        receipt_id = str(again["receipt_id"])
    elif kept is None and target is not None:
        project_id = target_project_id
        workspace_id = str(target["workspace_id"])
        resources, workflows = [], []
        receipt_id = str(again["receipt_id"]) if again is not None else None
    elif kept is None:
        title = str(spec.get("title") or "Easy Mode").strip()[:120]
        description = str(spec.get("blurb") or spec.get("prompt") or "").strip()[:2000] or None
        slug = _project_slug(title, dedupe_key)
        created = create_project(
            connection, settings, auth=auth, name=title, slug=slug, description=description,
            actor=actor, project_type="app")
        if str(created.get("org_id") or "") != org_id:
            raise FactoryLaunchError(
                409, "wrong_org", "The project was not created in this organization.")
        project_id = str(created["project_id"])
        workspace_id = str(created["workspace_id"])
        resources = _assign_resources(connection, settings, project_id,
                                      spec.get("resources") or [], actor=actor)
        workflows = _write_workflows(connection, project_id, spec.get("workflows") or [],
                                     actor=actor)
        receipt_id = str(again["receipt_id"]) if again is not None else None
    title = str(spec.get("title") or "Easy Mode").strip()[:120]
    return _launch_with_dedupe_key(
        connection, settings, org_id=org_id, actor=actor, auth=auth,
        dedupe_key=dedupe_key, spec=spec, project_id=project_id, workspace_id=workspace_id,
        resources=resources, workflows=workflows, receipt_id=receipt_id,
        again=again, builder=builder, jobs=jobs, budget=budget, allowed=allowed)


def _launch_with_dedupe_key(
    connection: Any,
    settings: Any,
    *,
    org_id: str,
    actor: str,
    auth: Any,
    dedupe_key: str,
    spec: dict[str, Any],
    project_id: str,
    workspace_id: str,
    resources: list[Any],
    workflows: list[Any],
    receipt_id: Optional[str],
    again: Optional[dict[str, Any]] = None,
    builder: Optional[Callable[..., dict[str, Any]]] = None,
    jobs: Any = None,
    budget: Any = None,
    allowed: bool = False,
) -> dict[str, Any]:
    title = str(spec.get("title") or "Easy Mode").strip()[:120]
    # A MODEL FIRST (stranger test r3, 2026-11-18): with no model connected the launch reserved
    # the project's whole cap for a build nothing could run, and the person read "the budget
    # cap is reached" at $0.00. Nothing is made, reserved or started; the sentence says what is
    # missing and links to it. Unknown is usable (model_readiness never holds work on a guess).
    from . import model_readiness

    if not model_readiness.usable_model(connection, settings, project_id):
        raise FactoryLaunchError(409, "needs_model", model_readiness.NO_MODEL_LAUNCH,
                                 fix_href=model_readiness.SETUP_MODELS_HREF)
    # THE BUILD'S REPOSITORY IS REAL: the connected GitHub repo, else the project's own local
    # repository (made here when it has none). Never a placeholder name nothing can clone. A
    # template launched into an existing project builds in that project's own repository.
    if spec.get("existing_project"):
        made_repo = False
    else:
        made_repo = _ensure_repository(connection, settings, project_id, spec, actor=actor)

    # The blurb and the prompt, before any reservation. A block or an unanswered ask stops here.
    if builder is None:
        builder = build_run
    try:
        # THE START ALONE MAY BE UNDONE (consent): the reservation and whatever the build start
        # wrote go back with this savepoint, the project made above stays.
        with connection.begin_nested():
            checked = before_builder_reserve(
                _launch_prompt(spec), allowed=bool(allowed),
                reserve=lambda: _reserve(connection, settings, project_id, title, budget))
            if checked["action"] == "block":
                raise FactoryLaunchError(409, "personal_data_blocked", str(checked["message"]))
            if checked["action"] != "allow":
                raise FactoryLaunchError(
                    409, "personal_data_ask",
                    "An email or a phone number has to be allowed before this launch reserves "
                    "a budget.")
            reservation_id = str(checked["reservation"])
            built = builder(
                connection, project_id=project_id, spec=spec, actor=actor, settings=settings,
                jobs=jobs, reservation_id=reservation_id)
    except ConsentRequired as asked:
        raise _keep_for_consent(
            connection, asked, org_id=org_id, workspace_id=workspace_id, project_id=project_id,
            dedupe_key=dedupe_key, spec=spec, resources=resources, workflows=workflows,
            receipt_id=receipt_id, allowed=bool(allowed)) from None
    except Exception:
        # The launch is refused (personal data blocked or asked, the budget, the start itself)
        # and its transaction rolls back with the project: the repository folder made on disk
        # for it on THIS call goes too, never left behind unbound.
        if made_repo:
            _drop_local_repos(project_id, settings)
        raise
    run_id = str((built or {}).get("run_id") or "").strip()
    returned_project = str((built or {}).get("project_id") or project_id).strip()
    if not run_id or returned_project != project_id:
        raise FactoryLaunchError(
            502, "launch_incomplete",
            "The launch did not return a project and a run.")
    body = {
        "project_id": project_id,
        "run_id": run_id,
        # A SPEC-FIRST LAUNCH's run is its Easy Mode MVP workflow run (the panel, then the
        # build): `stage` "spec" says the team is writing the spec before anything is built.
        **({"workflow_run_id": str(built["workflow_run_id"]), "stage": "spec"}
           if (built or {}).get("workflow_run_id") else {"stage": "build"}),
        "dedupe_key": dedupe_key,
        "reservation_id": reservation_id,
        "mode": spec["mode"],
        "template_id": spec.get("id"),
        "resources": resources,
        "workflows": workflows,
        "reused": False,
        "spec": _spec_view(spec),
    }
    if receipt_id is None:
        receipt, created_receipt = ReceiptService().create_or_reuse(
            connection, org_id, workspace_id, project_id,
            dedupe_key=dedupe_key, effect_kind="factory.launch",
            effect_scope={"mode": spec["mode"], "template_id": spec.get("id")},
            payload=body)
        receipt_id = str(receipt["id"])
        if not created_receipt and receipt.get("status") == "in_progress":
            # A kept launch whose project was removed meanwhile: this launch is the real one.
            connection.execute(text("UPDATE receipts SET project_id = :p WHERE id = :r"),
                               {"p": project_id, "r": receipt_id})
            created_receipt = True
    else:
        created_receipt = True        # the kept launch's own receipt: this is its real launch
    body["receipt_id"] = receipt_id
    if created_receipt:
        if again is not None:
            # A relaunch remembers the project it ran in (made again when it had been removed).
            connection.execute(text("UPDATE receipts SET project_id = :p WHERE id = :r"),
                               {"p": project_id, "r": receipt_id})
        ReceiptService().mark_succeeded(connection, receipt_id, None, body)
    return body


def _spec_view(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec.get("id"),
        "title": spec.get("title"),
        "prompt": spec.get("prompt"),
        "stack": spec.get("stack"),
        "toolchain": spec.get("toolchain"),
        "resources": list(spec.get("resources") or []),
        "workflows": list(spec.get("workflows") or []),
        "attachments": spec.get("attachments") or [],
        "github_repo": spec.get("github_repo"),
        "local_repo": spec.get("local_repo"),
        "spec_first": bool(spec.get("spec_first")),
        "spec_approval": spec.get("spec_approval"),
    }


def _keep_for_consent(
    connection: Any, asked: ConsentRequired, *, org_id: str, workspace_id: str, project_id: str,
    dedupe_key: str, spec: dict[str, Any], resources: list[Any], workflows: list[Any],
    receipt_id: Optional[str], allowed: bool = False,
) -> ConsentRequired:
    """The build asked for consent: keep the project (its agents, Passports and workflows) and
    one in-progress receipt that remembers the launch, and answer the same 409 with the project.

    The CALLER MUST COMMIT on this ConsentRequired (the API route does): the consent body names
    this project's agents, and the same launch sent again (same dedupe key) starts the build
    in it under the new grant."""
    body = dict(getattr(asked, "body", None) or {})
    # The agents' Passports belong to the kept project: issue them in this transaction, so the
    # grant the person creates names agents that exist (the savepoint took any issued during the
    # start back with it).
    try:
        from ..identity import consent as _consent
        from ..identity import passports as _passports

        with connection.begin_nested():
            _passports.upgrade_from_roles(
                org_id=org_id, db=connection,
                default_sponsor=_consent._first_owner(connection, org_id))  # noqa: SLF001
    except Exception:  # noqa: BLE001 — the start issues them again; the consent ask stands
        import logging

        logging.getLogger(__name__).warning("Passports for kept launch %s not issued", project_id,
                                            exc_info=True)
    kept = {
        "awaiting": "consent",
        "project_id": project_id,
        "workspace_id": workspace_id,
        "dedupe_key": dedupe_key,
        "spec": spec,
        "resources": resources,
        "workflows": workflows,
        "allowed": bool(allowed),
    }
    receipts = ReceiptService()
    if receipt_id is None:
        receipt, _ = receipts.create_or_reuse(
            connection, org_id, workspace_id, project_id,
            dedupe_key=dedupe_key, effect_kind="factory.launch",
            effect_scope={"mode": spec.get("mode"), "template_id": spec.get("id")},
            payload=kept)
        receipt_id = str(receipt["id"])
    else:
        # The kept receipt names the project the launch waits in (a re-ask after the project
        # it named was removed made a new one).
        # In progress again: a relaunch of an ended build that now waits for consent is a kept
        # launch (`_kept`), not the earlier succeeded one handed back by `_reuse`.
        connection.execute(text("UPDATE receipts SET project_id = :p, status = 'in_progress' "
                                "WHERE id = :r"),
                           {"p": project_id, "r": receipt_id})
    receipts.merge_result(connection, receipt_id, kept)
    body.update({
        "project_id": project_id,
        "dedupe_key": dedupe_key,
        "receipt_id": receipt_id,
        "created": True,
        "retry": {"project_id": project_id, "dedupe_key": dedupe_key},
    })
    return ConsentRequired(body)


def _kept(connection: Any, org_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    """The launch kept while it waited for consent (an in-progress receipt), or None."""
    existing = ReceiptService().get_by_dedupe_key(connection, org_id, dedupe_key)
    if not existing or existing.get("status") != "in_progress":
        return None
    result = existing.get("result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    if not isinstance(result, dict) or result.get("awaiting") != "consent":
        return None
    if not result.get("project_id") or not isinstance(result.get("spec"), dict):
        return None
    alive = connection.execute(text("SELECT 1 FROM projects WHERE id = :p"),
                               {"p": result["project_id"]}).first()
    if alive is None:
        return None
    return {**result, "receipt_id": existing.get("id")}


def kept_for_projects(connection: Any, project_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Kept Easy Mode launches waiting on consent, keyed by project_id."""
    ids = [str(p) for p in project_ids if p]
    if not ids:
        return {}
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT ON (project_id) id, org_id, project_id, dedupe_key, result
            FROM receipts
            WHERE project_id = ANY(:ids)
              AND effect_kind = 'factory.launch'
              AND status = 'in_progress'
              AND result->>'awaiting' = 'consent'
            ORDER BY project_id, created_at DESC, id DESC
            """
        ),
        {"ids": ids},
    ).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        pid = str(row["project_id"])
        result = row.get("result") or {}
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                continue
        if not isinstance(result, dict) or result.get("awaiting") != "consent":
            continue
        if str(result.get("project_id") or "") != pid or not isinstance(result.get("spec"), dict):
            continue
        dedupe = str(row.get("dedupe_key") or result.get("dedupe_key") or "")
        if not dedupe:
            continue
        out[pid] = {
            **result,
            "receipt_id": str(row["id"]),
            "org_id": str(row["org_id"]),
            "dedupe_key": dedupe,
        }
    return out


def kept_for_project(connection: Any, project_id: str) -> Optional[dict[str, Any]]:
    """The kept Easy Mode launch waiting for consent on this project, or None."""
    pid = str(project_id or "").strip()
    if not pid:
        return None
    return kept_for_projects(connection, [pid]).get(pid)


def resume(
    connection: Any,
    settings: Any,
    *,
    org_id: str,
    project_id: str,
    actor: str,
    auth: Any = None,
    jobs: Any = None,
    budget: Any = None,
    builder: Optional[Callable[..., dict[str, Any]]] = None,
    allowed: Optional[bool] = None,
) -> dict[str, Any]:
    """Resume a kept Easy Mode launch for `project_id` using its stored `dedupe_key`."""
    kept = kept_for_project(connection, project_id)
    if kept is not None and str(kept.get("org_id") or "") == str(org_id):
        dedupe_key = str(kept["dedupe_key"])
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": dedupe_key})
        previous = _reuse(connection, org_id, dedupe_key)
        if previous is not None:
            return previous
        kept_now = _kept(connection, org_id, dedupe_key)
        if kept_now is not None:
            return _launch_with_dedupe_key(
                connection, settings, org_id=org_id, actor=actor, auth=auth,
                dedupe_key=dedupe_key,
                spec=dict(kept_now["spec"]),
                project_id=str(kept_now["project_id"]),
                workspace_id=str(kept_now["workspace_id"]),
                resources=list(kept_now.get("resources") or []),
                workflows=list(kept_now.get("workflows") or []),
                receipt_id=str(kept_now["receipt_id"]),
                builder=builder, jobs=jobs, budget=budget,
                allowed=bool(kept_now.get("allowed")) if allowed is None else bool(allowed),
            )
    # Idempotency: if the project's factory.launch receipt already succeeded and its run is live,
    # return that launch result rather than failing on a double press.
    row = connection.execute(
        text(
            """
            SELECT dedupe_key FROM receipts
            WHERE org_id = :org AND project_id = :pid
              AND effect_kind = 'factory.launch' AND status = 'succeeded'
            ORDER BY updated_at DESC, id DESC LIMIT 1
            """
        ),
        {"org": org_id, "pid": project_id},
    ).first()
    if row is not None and row[0]:
        previous = _reuse(connection, org_id, str(row[0]))
        if previous is not None:
            return previous
    raise FactoryLaunchError(
        409, "nothing_to_start",
        "This project has no Easy Mode launch waiting for consent.",
    )


#: Said when a project ends up with no repository to build in. Raised before anything starts.
NO_REPOSITORY = (
    "This project has no repository to build in: BOSS could not make one on this machine (git "
    "is missing or its repository folder is not writable), and no GitHub repository was chosen. "
    "Nothing was started.")


def _ensure_repository(
    connection: Any, settings: Any, project_id: str, spec: dict[str, Any], *, actor: str,
) -> bool:
    """Name the repository the build runs against in the spec: the GitHub repo the person chose,
    else the project's local primary repository, made by the one helper new local projects use
    (projects.ensure_sample_code). No repository at all is a refusal in words, before the start.
    True when this call made the repository on disk."""
    if spec.get("github_repo"):
        return False
    from . import local_projects
    from .projects import ensure_sample_code

    made = False
    bound = local_projects.local_binding(connection, project_id)
    if bound is None:
        made = ensure_sample_code(connection, settings, project_id, actor=actor,
                                  stack=str(spec.get("stack") or "") or None) is not None
        bound = local_projects.local_binding(connection, project_id)
    if bound is None or not bound.get("full_name"):
        raise FactoryLaunchError(409, "no_repository", NO_REPOSITORY)
    spec["local_repo"] = str(bound["full_name"])
    return made


def _drop_local_repos(project_id: str, settings: Any) -> None:
    """Remove the project's repository folder (the launch that made it was refused)."""
    from . import local_repo

    try:
        local_repo.delete_project_repos(project_id, settings)
    except Exception:  # noqa: BLE001 — the refusal is the answer; a leftover folder is logged
        import logging

        logging.getLogger(__name__).warning("could not remove the repository folder of %s",
                                            project_id, exc_info=True)


#: Who approves the panel's PRD on a launch (meeting_service.spec_approval_of): the person's
#: start (the light lane), or the person after reading it ("Review the plan first").
SPEC_BY_START = "start"
SPEC_BY_PERSON = "person"

#: Launch modes that keep the small path (one build, no panel), as New work's Bug does (#191).
SMALL_PATH_MODES = frozenset({"bug", "bugfix", "fix", "small_change"})


def spec_first(mode: Any) -> bool:
    """Whether a launch of this mode convenes the panel before it builds."""
    return str(mode or "").strip().lower() not in SMALL_PATH_MODES


def build_run(
    connection: Any,
    *,
    project_id: str,
    spec: dict[str, Any],
    actor: str,
    settings: Any,
    jobs: Any,
    reservation_id: str,
) -> dict[str, str]:
    """Start the build. A spec-first launch (`spec["spec_first"]`) starts the Easy Mode MVP
    workflow — the panel writes the spec, then the build runs from it (`spec_first_run`); a small
    change starts the code-task build directly. Tests replace this; it does not call a model."""
    if spec.get("spec_first"):
        return spec_first_run(connection, project_id=project_id, spec=spec, actor=actor,
                              settings=settings, jobs=jobs, reservation_id=reservation_id)
    from .code_task_service import start_code_task

    github = str(spec.get("github_repo") or "")
    local = str(spec.get("local_repo") or "")
    owner, _, repo = (github or local).partition("/")
    if not owner or not repo:
        raise FactoryLaunchError(409, "no_repository", NO_REPOSITORY)
    title = str(spec.get("title") or "Easy Mode")[:180]
    run_id = start_code_task(
        connection, project_id=project_id, task_id=None, title=title,
        instructions=str(spec.get("prompt") or title),
        acceptance_criteria=[], test_commands=[],
        owner=owner, repo=repo, base_branch=None,
        finalize_ctx={"factory_launch": True, "template_id": spec.get("id"),
                      "mode": spec.get("mode"), "reservation_id": reservation_id,
                      "actor": actor,
                      # A template built into an existing project is still a NEW app there.
                      **({"new_app": True} if spec.get("existing_project") else {})},
        # Its pull request and merge card open like every product build's: `open_pr=False` is
        # for evaluation runs, and it left Easy Mode builds completed with no PR (crun_5b11643d).
        jobs=jobs, repo_host=None if github else "local")
    if not run_id:
        return {"project_id": project_id}
    # THE LAUNCH'S HOLD IS THE BUILD'S (a reservation never blocks the job it was made for): it
    # is recorded as the run's reservation, so the build's job is admitted against it
    # (jobs._own_reservations) and the run settles it when it starts, before reserving its own.
    from . import build_budget

    build_budget.bind(connection, str(run_id), reservation_id)
    return {"project_id": project_id, "run_id": str(run_id)}


def spec_first_run(
    connection: Any,
    *,
    project_id: str,
    spec: dict[str, Any],
    actor: str,
    settings: Any,
    jobs: Any,
    reservation_id: str,
) -> dict[str, str]:
    """THE TEAM WRITES THE SPEC, THEN BUILDS IT (operator, 2026-11-19: "one of the main features
    is to have a spec for all the features / project before starting anything").

    Starts the project's Easy Mode MVP workflow (workflow_catalogue.EASY_MODE_WORKFLOW): a compact
    panel discusses the brief (the template's prompt, the person's words, the attachments) and
    writes minutes and a lean PRD; the PRD is approved by this start (`spec_approval` "start",
    recorded as "approved by your Easy Mode start") unless the person chose to review it; the
    build runs from it, then the preview and the deploy. A panel that fails or overruns builds
    from the brief and says why (workflow_capability_runner._panel_fallback).

    One consent covers the delivery: the workflow start asks for the grant its steps need
    (ConsentRequired, kept by the launch like the direct build's). The launch's budget hold was
    the check that a build may start; it is released here, because each step reserves its own."""
    from . import workflow_catalogue, workflows as wf

    github = str(spec.get("github_repo") or "")
    local = str(spec.get("local_repo") or "")
    if not (github or local):
        raise FactoryLaunchError(409, "no_repository", NO_REPOSITORY)
    key = workflow_catalogue.EASY_MODE_WORKFLOW
    workflow_catalogue.seed(connection, project_id, actor="boss", only=frozenset({key}))
    running = wf.running_version(connection, project_id, key)
    if not running or not running.get("version_id"):
        raise FactoryLaunchError(
            409, "no_workflow",
            "BOSS could not install its Easy Mode workflow in this project, so nothing was "
            "started.")
    title = str(spec.get("title") or "Easy Mode")[:180]
    review_first = str(spec.get("spec_approval") or "") == SPEC_BY_PERSON
    run_input = {
        "title": title,
        "directive": str(spec.get("prompt") or title),
        "repository": github or local,
        # "Review the plan first": stricter, so a plain input (the panel's PRD waits).
        **({"spec_approval": SPEC_BY_PERSON} if review_first else {}),
    }
    if not spec.get("existing_project"):
        _staging_ships_without_asking(connection, project_id, actor)
    run = wf.start_run(
        connection, project_id=project_id, actor=actor, input_data=run_input,
        dedupe_key=f"easy-mode:{project_id}:{reservation_id}",
        workflow_version_id=str(running["version_id"]),
        # A template built into an existing project (spec["existing_project"], server-set) is a
        # NEW app in that project, never a change to the app already there: declared through
        # start_run (workflows.NEW_APP_INPUT is server-only), whoever was allowed to launch it.
        # Inherently so — a launch names a template, never an existing app — and the declaration
        # only stops OTHER apps' branches blocking it: a launch of an app already built here (the
        # same recorded app id) is still refused by the empty-checkout guard (build_checkout).
        declared_new_app=bool(spec.get("existing_project")),
        # THE START APPROVES THE PRD — server-only, with who started it, and only when that
        # person may approve a PRD here (an enforced request asks the PDP).
        spec_approved_by_start=None if review_first else _start_approver(connection, project_id, actor))
    run_id = str((run or {}).get("id") or "")
    if not run_id:
        _release_hold(connection, settings, project_id, reservation_id)
        return {"project_id": project_id}
    from .workflow_readiness import note_unready_start

    try:
        with connection.begin_nested():
            note_unready_start(connection, settings, project_id, actor, run_id,
                               version_id=str(running["version_id"]), input_data=run_input)
    except Exception:  # noqa: BLE001 — a note that cannot be computed never stops the start
        import logging

        logging.getLogger(__name__).warning("readiness note for %s failed", run_id, exc_info=True)
    _release_hold(connection, settings, project_id, reservation_id)
    return {"project_id": project_id, "run_id": run_id, "workflow_run_id": run_id}


def _release_hold(connection: Any, settings: Any, project_id: str, reservation_id: str) -> None:
    """The launch's hold was the check that a build may start; each step reserves its own."""
    try:
        from .budget import BudgetGovernor

        BudgetGovernor(settings).release(
            connection, project_id, reservation_id,
            "Easy Mode started its workflow; each step reserves its own budget")
    except Exception:  # noqa: BLE001 — a hold left to expire is not a failed start
        import logging

        logging.getLogger(__name__).warning("launch hold %s not released", reservation_id,
                                            exc_info=True)


def _start_approver(connection: Any, project_id: str, actor: str) -> Optional[str]:
    """Who approves the panel's PRD by starting Easy Mode: the person whose request this is,
    when they may approve a PRD on this project. Under an enforced request the PDP answers
    (decision:approve); without one, the start's actor. None means a person approves it."""
    from ..identity import lineage, viewer

    # In the launch's own transaction: it holds the org's audit lock and the uncommitted project.
    if viewer.decide(project_id, "decision:approve", db=connection) is False:
        return None
    ctx = lineage.current_context()
    person = str(getattr(getattr(ctx, "principal", None), "id", "") or "") if ctx else ""
    return person or str(actor or "").strip() or None


#: Recorded on the staging policy an Easy Mode start sets.
STAGING_POLICY_NOTE = ("Set by your Easy Mode start: a staging deploy ships without asking. "
                       "Production still asks.")


def _staging_ships_without_asking(connection: Any, project_id: str, actor: str) -> None:
    """NO DEPLOY APPROVAL BELOW PRODUCTION (operator, 2026-11-19: "one consent covers the whole
    delivery workflow"). The Easy Mode MVP workflow's deploy is a staging deploy that follows the
    environment's policy, and no policy row means ask; so the start sets this project's staging
    policy to ship without asking — only when the project has none. A policy someone set is
    never changed, and production is untouched."""
    from .deploy_policy import deploy_policy, set_approval_policy

    if deploy_policy(connection, project_id, "staging").get("policy_id"):
        return
    set_approval_policy(connection, project_id=project_id, environment="staging",
                        requires_approval=False, actor=actor or "boss",
                        note=STAGING_POLICY_NOTE)


def resolve_spec(
    prompt: str, template_id: Optional[str], root: Optional[Path] = None,
) -> tuple[dict[str, Any], str]:
    """What the launch builds.

    - A pasted spec (the template schema as YAML) is the spec.
    - A template the person picked (`template_id` in the catalogue) is the spec — its title,
      stack, resources and workflows — with the person's words, when they wrote some, as the
      prompt. The build home sends both: picking a card fills the brief, and the brief used to
      win outright, so the project was named after its first 80 characters and the template's
      stack never reached the repository's .gitignore (stranger test r3, 2026-11-18).
    - Other words are a prose spec, titled from their first words.
    - Nothing at all picks the first template, else the built-in spec."""
    text_prompt = str(prompt or "").strip()
    catalogue = list_templates(root)
    if text_prompt:
        parsed = _parse_spec_text(text_prompt)
        if parsed is not None:
            _require_buildable(parsed)
            return parsed, "prompt"
    match = None
    if template_id:
        match = next((item for item in catalogue["templates"] if item["id"] == template_id), None)
        if match is None and not text_prompt:
            raise FactoryLaunchError(404, "unknown_template", f"No template {template_id}.")
    if match is not None:
        if match.get("needs"):
            raise FactoryLaunchError(409, "toolchain_not_enabled",
                                     "Not started: " + match["needs"]["message"])
        spec = _plain(match)
        if text_prompt:
            spec["prompt"] = text_prompt
        return spec, "template"
    if text_prompt:
        return _prose_spec(text_prompt), "prompt"
    buildable = [item for item in catalogue["templates"] if item.get("available", True)]
    if buildable:
        return _plain(buildable[0]), "lucky"
    return dict(BUILTIN_SPEC), "lucky"


class UnbuildableSpec(ValueError):
    """A spec in the template schema that names a platform or toolchain BOSS cannot build."""


def _template(loaded: Any, catalogue: bool = False) -> dict[str, Any]:
    """A spec in the template schema. A catalogue file must name its toolchain and must not need
    a platform no image carries; a pasted spec may leave the toolchain out."""
    if not isinstance(loaded, dict):
        raise ValueError("a template is a mapping")
    toolchain = loaded.get("toolchain", "" if not catalogue else None)
    if toolchain is None:
        raise ValueError("missing toolchain")
    if not isinstance(toolchain, str):
        raise ValueError("toolchain")
    missing = [field for field in _TEMPLATE_FIELDS
               if field != "toolchain" and field not in loaded]
    if missing:
        raise ValueError("missing " + ", ".join(missing))
    for field in ("id", "title", "blurb", "category", "prompt", "stack"):
        if not isinstance(loaded[field], str):
            raise ValueError(field)
    if not loaded["id"].strip() or not loaded["title"].strip():
        raise ValueError("id and title")
    resources = loaded["resources"]
    if not isinstance(resources, list) or not all(isinstance(item, str) for item in resources):
        raise ValueError("resources")
    workflows = loaded["workflows"]
    if not isinstance(workflows, list) or not workflows:
        raise ValueError("workflows")
    # A WHOLE SPEC THAT ASKS FOR WHAT NO IMAGE CARRIES is refused, never quietly built as prose.
    hit = next((m.group(0) for field in ("category", "stack", "toolchain")
                for m in [UNBUILDABLE_PLATFORM.search(str(loaded.get(field) or ""))] if m), None)
    if hit:
        raise UnbuildableSpec(hit)
    if toolchain and toolchain not in TEMPLATE_TOOLCHAINS:
        raise UnbuildableSpec(toolchain)
    return {
        "id": loaded["id"].strip(),
        "title": loaded["title"].strip(),
        "blurb": loaded["blurb"],
        "category": loaded["category"],
        "prompt": loaded["prompt"],
        "stack": loaded["stack"],
        "toolchain": toolchain,
        "resources": list(resources),
        "workflows": [_workflow_item(item) for item in workflows],
    }


def _workflow_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("workflow")
    name = item.get("name")
    steps = item.get("steps")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("workflow name")
    if not isinstance(steps, list) or not steps:
        raise ValueError("workflow steps")
    cleaned = []
    for step in steps:
        if isinstance(step, str) and step.strip():
            cleaned.append(step.strip())
            continue
        if isinstance(step, dict):
            label = str(step.get("name") or step.get("title") or step.get("id") or "").strip()
            if label:
                cleaned.append({"name": label})
                continue
        raise ValueError("step")
    return {"name": name.strip(), "steps": cleaned}


def _parse_spec_text(prompt: str) -> Optional[dict[str, Any]]:
    try:
        loaded = yaml.safe_load(prompt)
    except yaml.YAMLError:
        return None
    try:
        return _template(loaded)
    except UnbuildableSpec as unbuildable:
        raise FactoryLaunchError(
            409, "platform_not_buildable",
            f"Not started: this spec asks for {str(unbuildable)!r}, which BOSS cannot build. BOSS "
            "builds web apps, games and services in Python and Node, and in Go or Java once that "
            "toolchain is turned on; it does not build Android or iOS apps or game-engine "
            "projects (docs/community/SUPPORTED_STACKS.md).") from None
    except ValueError:
        return None


#: A prose brief's project name: its first words, cut at a word boundary near this length.
TITLE_CHARS = 40


def prose_title(brief: str, limit: int = TITLE_CHARS) -> str:
    """The project's name from a brief: its first line, cut at the last word boundary within
    `limit` characters (a single longer word is cut at `limit`), trailing punctuation dropped.
    "Easy Mode" when there is nothing to name it by."""
    line = (str(brief or "").strip().splitlines() or [""])[0].strip()
    if len(line) > limit:
        cut = line[:limit + 1]
        space = cut.rstrip().rfind(" ")
        line = cut[:space] if space >= limit // 2 else line[:limit]
    line = line.rstrip(" ,;:.-–—")
    return line if len(line) >= 2 else "Easy Mode"


def _prose_spec(prompt: str) -> dict[str, Any]:
    title = prose_title(prompt)
    return {
        "id": "prompt",
        "title": title,
        "blurb": "",
        "category": "app",
        "prompt": prompt.strip(),
        "stack": "",
        "resources": [],
        "workflows": [{"name": "Build", "steps": ["Build it"]}],
    }


def _fold_attachments(spec: dict[str, Any], attachments: list[dict[str, Any]]) -> dict[str, Any]:
    folded = dict(spec)
    folded["attachments"] = [
        {"id": item["id"], "name": item["name"], "kind": item["kind"],
         "text_extract": item["text_extract"]}
        for item in attachments
    ]
    if not attachments:
        return folded
    lines = ["", "Attachments:"]
    for item in attachments:
        extract = item["text_extract"]
        if extract:
            lines.append(f"- {item['name']} ({item['kind']}): {extract}")
        else:
            lines.append(f"- {item['name']} ({item['kind']})")
    folded["prompt"] = str(folded.get("prompt") or "").rstrip() + "\n" + "\n".join(lines)
    return folded


def _load_attachments(connection: Any, org_id: str, ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    rows = connection.execute(text(
        """
        SELECT id, name, kind, text_extract
        FROM factory_launch_attachments
        WHERE org_id = :org AND id = ANY(:ids)
        """), {"org": org_id, "ids": ids}).mappings().all()
    by_id = {str(row["id"]): row for row in rows}
    if len(by_id) != len(set(ids)):
        raise FactoryLaunchError(
            404, "unknown_attachment",
            "That attachment is not in this organization.")
    return [
        {"id": str(by_id[item]["id"]), "name": by_id[item]["name"], "kind": by_id[item]["kind"],
         "text_extract": by_id[item]["text_extract"] or ""}
        for item in ids
    ]


def _connected_drive(connection: Any, org_id: str, source_id: str) -> Optional[str]:
    """The connection's name when this org already has that Google source. No network."""
    row = connection.execute(text(
        """
        SELECT c.connection_key
        FROM integration_connections c
        JOIN integration_definitions d ON d.id = c.integration_definition_id
        JOIN projects p ON p.id = c.project_id
        JOIN workspaces w ON w.id = p.workspace_id
        WHERE c.status = 'active'
          AND d.integration_key IN ('google', 'google_drive', 'google-drive')
          AND w.org_id = :org
          AND (c.id = :src OR c.connection_key = :src)
        LIMIT 1
        """), {"org": org_id, "src": source_id}).first()
    if row is None:
        return None
    return str(row[0] or source_id)


def _require_github_repo(connection: Any, org_id: str, full_name: str) -> None:
    """The repo must already be on the org's registry. This does not call GitHub."""
    if "/" not in full_name or full_name.startswith("/") or full_name.endswith("/"):
        raise FactoryLaunchError(
            422, "github_repo", "Name the repository as owner/name.")
    wanted = norm_repo(full_name)
    row = connection.execute(text(
        """
        SELECT allowed, reachable, archived
        FROM github_repos
        WHERE org_id = :org AND lower(full_name) = :name
        LIMIT 1
        """), {"org": org_id, "name": wanted}).mappings().first()
    if row is None or not row["allowed"] or not row["reachable"] or row["archived"]:
        raise MultiRepoError(409, "github_not_connected", NOT_CONNECTED)


def _assign_resources(
    connection: Any, settings: Any, project_id: str, resources: list[Any], *, actor: str,
) -> list[str]:
    from .work_orchestration import WorkOrchestrationService

    service = WorkOrchestrationService(settings)
    assigned: list[str] = []
    for raw in resources:
        kind = str(raw or "").strip()
        if not kind:
            continue
        row = service.upsert_quota(
            connection, project_id=project_id, scope_type="project", scope_id=project_id,
            resource_kind=kind, reserved_min=1, soft_limit=1, hard_limit=1, actor=actor)
        assigned.append(str(row["resource_kind"]))
    return assigned


def _write_workflows(
    connection: Any, project_id: str, workflows: list[Any], *, actor: str,
) -> list[dict[str, str]]:
    written: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(workflows, start=1):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"Workflow {index}").strip()
        base = "launch-" + _slug(name, f"workflow-{index}")
        key = base[:80]
        n = 2
        while key in seen:
            key = f"{base[:70]}-{n}"
            n += 1
        seen.add(key)
        try:
            definition = create_definition(
                connection, project_id=project_id, workflow_key=key, name=name[:200],
                description="Written by Easy Mode.", actor=actor,
                namespace=PRODUCT_NAMESPACE, made_by_boss=True)
            publish_version(
                connection, project_id=project_id, definition_id=definition["id"],
                spec=_step_spec(item.get("steps") or []), actor=actor)
        except WorkflowError as error:
            raise FactoryLaunchError(422, "workflow_refused", str(error)) from error
        written.append({"id": definition["id"], "name": name, "workflow_key": key})
    return written


def _step_spec(steps: list[Any]) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    source = steps or ["Build it"]
    for index, step in enumerate(source, start=1):
        if isinstance(step, str):
            title = step.strip()
        elif isinstance(step, dict):
            title = str(step.get("name") or step.get("title") or "").strip()
        else:
            title = ""
        if not title:
            title = f"Step {index}"
        base = _slug(title, f"step-{index}")[:40]
        ident = base
        n = 2
        while ident in seen:
            ident = f"{base[:30]}-{n}"
            n += 1
        seen.add(ident)
        normalized.append({
            "id": ident, "kind": "task", "title": title[:80], "binding": {}, "next": [],
        })
    for index, step in enumerate(normalized[:-1]):
        step["next"] = [normalized[index + 1]["id"]]
    # entry_step is required to publish. The first step is the start; next walks the rest.
    return {"entry_step": normalized[0]["id"], "steps": normalized}


def _launch_prompt(spec: dict[str, Any]) -> str:
    """Blurb and prompt together, so a clean blurb cannot hide the prompt."""
    blurb = str(spec.get("blurb") or "").strip()
    body = str(spec.get("prompt") or "").strip()
    if blurb and body and blurb != body:
        return f"{blurb}\n{body}"
    return blurb or body


def _reserve(connection: Any, settings: Any, project_id: str, title: str, budget: Any) -> str:
    from . import build_budget
    from .budget import BudgetGovernor

    governor = budget if budget is not None else BudgetGovernor(settings)
    amount = build_budget.reservation_estimate(connection, settings, project_id)
    reservation = governor.reserve(
        connection, project_id, category=build_budget.CATEGORY,
        amount_usd_micros=amount, reason=f"factory launch: {title[:80]}",
        expires_in_minutes=build_budget.RESERVATION_MINUTES)
    if reservation.get("status") != "reserved" or not reservation.get("reservation_id"):
        message = reservation.get("message") or "The budget refused this launch."
        raise FactoryLaunchError(409, str(reservation.get("refusal") or "budget_refused"),
                                 str(message))
    return str(reservation["reservation_id"])


#: How long the same launch hands back its earlier answer when its run is not (yet) a row BOSS
#: can read — a run of an ended status is never handed back, whatever its age.
REUSE_WINDOW_MINUTES = 10


def _succeeded(connection: Any, org_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    """The launch's succeeded receipt and its result, or None."""
    existing = ReceiptService().get_by_dedupe_key(connection, org_id, dedupe_key)
    if not existing or existing.get("status") != "succeeded":
        return None
    result = existing.get("result") or {}
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return None
    if not isinstance(result, dict):
        return None
    if not result.get("project_id") or not result.get("run_id"):
        return None
    return {"receipt": existing, "result": result}


def _still_live(connection: Any, receipt_id: str, run_id: str) -> bool:
    """The earlier launch is still the answer: its run has not ended, or (a run BOSS cannot see
    yet) it answered moments ago.

    Before 2026-11-18 a launch was reused forever: Recipe box's run failed on a platform bug,
    and pressing Easy Mode on Recipe box again handed back that dead run. An ENDED run is never
    the answer, however recent: a double press waits on the launch's lock and finds the run
    live, and the resend after "Confirm it's you" follows a refusal, not a launch."""
    from .code_task_service import TERMINAL_RUN_STATUSES
    from .workflows import TERMINAL_RUN_STATES

    if str(run_id).startswith("wfr_"):
        # A spec-first launch's run is its workflow run (the panel, then the build).
        run = connection.execute(text("SELECT status FROM workflow_runs WHERE id = :id"),
                                 {"id": run_id}).first()
        terminal = TERMINAL_RUN_STATES
    else:
        run = connection.execute(text("SELECT status FROM code_task_runs WHERE id = :id"),
                                 {"id": run_id}).first()
        terminal = TERMINAL_RUN_STATUSES
    if run is not None:
        return str(run[0] or "") not in terminal
    row = connection.execute(text(
        "SELECT (now() - updated_at) < make_interval(mins => :mins) AS fresh "
        "FROM receipts WHERE id = :r"), {"r": receipt_id, "mins": REUSE_WINDOW_MINUTES}).first()
    return bool(row is not None and row[0])


def _reuse(connection: Any, org_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    """The earlier launch's answer, while it is still the answer (`_still_live`), or None."""
    found = _succeeded(connection, org_id, dedupe_key)
    if found is None:
        return None
    receipt, result = found["receipt"], found["result"]
    receipt_id = str(receipt.get("id") or result.get("receipt_id") or "")
    if not _still_live(connection, receipt_id, str(result["run_id"])):
        return None
    return {**result, "reused": True, "receipt_id": receipt_id}


def _relaunch(connection: Any, org_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
    """The same launch, pressed after its build ended: its receipt, and its project when that
    still exists (with the resources and workflows it was given), to start a new run in. None
    when the launch never succeeded."""
    found = _succeeded(connection, org_id, dedupe_key)
    if found is None:
        return None
    receipt, result = found["receipt"], found["result"]
    again: dict[str, Any] = {"receipt_id": str(receipt["id"])}
    row = connection.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                             {"p": result["project_id"]}).first()
    if row is not None:
        again.update({"project_id": str(result["project_id"]), "workspace_id": str(row[0]),
                      "resources": list(result.get("resources") or []),
                      "workflows": list(result.get("workflows") or [])})
    return again


def _dedupe_key(
    org_id: str, prompt: str, mode: str, template_id: Optional[str],
    attachment_ids: list[str], github_repo: str, who: str = "", review_plan: bool = False,
    target_project_id: Optional[str] = None,
) -> str:
    raw = json.dumps({
        # Only when asked, so every launch made before the option keeps its key.
        **({"review_plan": True} if review_plan else {}),
        **({"target_project": target_project_id} if target_project_id is not None else {}),
        "org": org_id,
        "who": who,
        "prompt": str(prompt or ""),
        "mode": mode,
        "template_id": template_id or "",
        "attachment_ids": sorted(attachment_ids),
        "github_repo": norm_repo(github_repo) if github_repo else "",
    }, sort_keys=True)
    return "factory.launch:" + hashlib.sha256(raw.encode()).hexdigest()


def _project_slug(title: str, dedupe_key: str) -> str:
    base = _slug(title, "launch")[:40].strip("-")
    digest = hashlib.sha256(dedupe_key.encode()).hexdigest()[:8]
    slug = f"{base}-{digest}"
    return slug[:63].strip("-")


def _slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug or fallback


def _filename(name: str) -> str:
    cleaned = Path(str(name or "")).name.strip()[:200]
    return cleaned or "attachment"


def _extract(kind: str, data: bytes) -> str:
    if kind == "image" or b"\x00" in data:
        return ""
    try:
        text_value = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return ""
    text_value = text_value.replace("\x00", "").strip()
    return text_value[:TEXT_EXTRACT_LIMIT]
