"""Easy Mode: one launch creates a project and starts its build (BOSSAPP-1274).

Empty prompt picks a concrete spec (the first template file by id, else a built-in spec)
and then runs the same path as a prompt the person wrote. A non-empty prompt is the spec.
The path creates the project, assigns the resources the spec names, writes the workflows
the spec names, reserves a budget, writes one receipt, and only then starts the build.
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
from typing import Any, Callable, Optional

import yaml
from sqlalchemy import text

from ..identity.consent import ConsentRequired
from ..multirepo.common import MultiRepoError, norm_repo
from ..multirepo.registry import NOT_CONNECTED
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

_TEMPLATE_FIELDS = ("id", "title", "blurb", "category", "prompt", "stack", "resources", "workflows")

BUILTIN_SPEC: dict[str, Any] = {
    "id": "lucky-notes",
    "title": "A small notes app",
    "blurb": "A list of notes and an editor, on one page.",
    "category": "app",
    "prompt": "Build a small notes app: a list of notes and an editor. One page, no accounts.",
    "stack": "vite-react",
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


def list_templates(root: Optional[Path] = None) -> dict[str, Any]:
    """The catalogue on disk. A file that is not the schema is skipped and counted, not fatal."""
    directory = Path(root) if root is not None else TEMPLATE_DIR
    if not directory.is_dir():
        return {"templates": [], "skipped": 0}
    found: list[dict[str, Any]] = []
    skipped = 0
    seen: set[str] = set()
    for path in sorted(directory.glob("*.yaml")):
        try:
            item = _template(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, yaml.YAMLError, ValueError):
            skipped += 1
            continue
        if item["id"] in seen:
            skipped += 1
            continue
        seen.add(item["id"])
        found.append(item)
    found.sort(key=lambda item: item["id"])
    return {"templates": found, "skipped": skipped}


def store_attachments(
    connection: Any,
    *,
    org_id: str,
    uploads: list[tuple[str, str, str, bytes]],
    drive_source_id: str = "",
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
                id, org_id, kind, name, media_type, byte_size, text_extract, drive_source_id
            ) VALUES (
                :id, :org, :kind, :name, :media, :size, :extract, :drive
            )
            """),
            {"id": attachment_id, "org": org_id, "kind": item["kind"], "name": item["name"],
             "media": item["media_type"], "size": item["byte_size"],
             "extract": item["text_extract"], "drive": item["drive_source_id"]})
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
    dedupe_key = _dedupe_key(org_id, prompt, asked, template_id, ids, repo, who)
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": dedupe_key})
    previous = _reuse(connection, org_id, dedupe_key)
    if previous is not None:
        return previous

    kept = _kept(connection, org_id, dedupe_key)
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
        attachments = _load_attachments(connection, org_id, ids)
        spec = _fold_attachments(spec, attachments)
        if repo:
            _require_github_repo(connection, org_id, repo)
            spec["github_repo"] = repo
        spec["mode"] = asked or origin

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
        receipt_id = None
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
    # repository (made here when it has none). Never a placeholder name nothing can clone.
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
            receipt_id=receipt_id) from None
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
        ReceiptService().mark_succeeded(connection, receipt_id, None, body)
    return body


def _spec_view(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": spec.get("id"),
        "title": spec.get("title"),
        "prompt": spec.get("prompt"),
        "stack": spec.get("stack"),
        "resources": list(spec.get("resources") or []),
        "workflows": list(spec.get("workflows") or []),
        "attachments": spec.get("attachments") or [],
        "github_repo": spec.get("github_repo"),
        "local_repo": spec.get("local_repo"),
    }


def _keep_for_consent(
    connection: Any, asked: ConsentRequired, *, org_id: str, workspace_id: str, project_id: str,
    dedupe_key: str, spec: dict[str, Any], resources: list[Any], workflows: list[Any],
    receipt_id: Optional[str],
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
        connection.execute(text("UPDATE receipts SET project_id = :p WHERE id = :r"),
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
    """Start the existing code-task build. Tests replace this; it does not call a model itself."""
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
                      "actor": actor},
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
            return parsed, "prompt"
    match = None
    if template_id:
        match = next((item for item in catalogue["templates"] if item["id"] == template_id), None)
        if match is None and not text_prompt:
            raise FactoryLaunchError(404, "unknown_template", f"No template {template_id}.")
    if match is not None:
        spec = dict(match)
        if text_prompt:
            spec["prompt"] = text_prompt
        return spec, "template"
    if text_prompt:
        return _prose_spec(text_prompt), "prompt"
    if catalogue["templates"]:
        return dict(catalogue["templates"][0]), "lucky"
    return dict(BUILTIN_SPEC), "lucky"


def _template(loaded: Any) -> dict[str, Any]:
    if not isinstance(loaded, dict):
        raise ValueError("a template is a mapping")
    missing = [field for field in _TEMPLATE_FIELDS if field not in loaded]
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
    return {
        "id": loaded["id"].strip(),
        "title": loaded["title"].strip(),
        "blurb": loaded["blurb"],
        "category": loaded["category"],
        "prompt": loaded["prompt"],
        "stack": loaded["stack"],
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


def _reuse(connection: Any, org_id: str, dedupe_key: str) -> Optional[dict[str, Any]]:
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
    return {**result, "reused": True, "receipt_id": existing.get("id") or result.get("receipt_id")}


def _dedupe_key(
    org_id: str, prompt: str, mode: str, template_id: Optional[str],
    attachment_ids: list[str], github_repo: str, who: str = "",
) -> str:
    raw = json.dumps({
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
