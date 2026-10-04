"""WHAT APPROVING A GATED STEP WILL ACTUALLY DO — shown before anybody presses Approve.

Found running the bundled examples live: the newsletter's send step was held for a person
with "Email the newsletter: weekly_newsletter · run 4715a965", a recommendation of
`{environment: "staging", capability: ...}` and the buttons "Approve and retry" / "Keep
blocked". The approver could not see who the email went to, what it said, which tickets would
be created or what title a video would be published under. An approval of something you cannot
see is a rubber stamp.

So when a gate opens, the step's inputs are resolved exactly as the runner will resolve them
when it runs (start inputs and upstream outputs), and turned into a PREVIEW:

  * a typed preview per skill kind, looked up in `PREVIEWERS` by skill id — an email reads as
    an email, tickets as a list, a publish as a small card;
  * a generic fallback for every other skill, custom tool or capability: the resolved inputs
    as labelled fields. A newly gated skill therefore gets a preview without anybody writing
    one.

Secrets never reach a preview: a field whose NAME says it is a credential is hidden, and any
value that LOOKS like one (API keys, bearer tokens, private keys, JWTs) is masked wherever it
appears, with a count so the card can say something was hidden.

THE APPROVAL IS BOUND TO THE CONTENT. `content_hash` is computed over the real resolved inputs
(never the redacted ones). The decision stores it; an approval may name the hash it saw, and the
runner refuses to run a step whose inputs no longer hash to what was approved — so a retried
upstream step cannot slip different words into an email somebody approved.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Optional

#: The longest body a preview carries in full. A newsletter is a few thousand characters; this
#: is a ceiling against a runaway upstream step, not a summary length.
MAX_BODY_CHARS = 60_000
#: How much of each ticket's description the list shows.
EXCERPT_CHARS = 280
#: How much of one generic field is shown.
MAX_FIELD_CHARS = 4_000
MAX_ITEMS = 50

REDACTED = "[hidden]"

# ---------------------------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------------------------


def content_hash(inputs: Any) -> str:
    """A stable hash of exactly what the step will be given."""
    canonical = json.dumps(inputs, sort_keys=True, default=str, separators=(",", ":"),
                           ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------------------------

_SECRET_WORDS = frozenset({
    "secret", "secrets", "token", "password", "passwd", "pwd", "passphrase", "credential",
    "credentials", "apikey", "authorization", "cookie", "bearer", "signature",
})
_SECRET_PAIRS = ("api_key", "private_key", "access_key", "secret_key", "client_secret",
                 "auth_header", "session_key", "refresh_token", "access_token", "id_token")

_SECRET_VALUE = re.compile(
    r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"
    r"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\bxox[abprs]-[A-Za-z0-9\-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_\-]{30,}"
    r"|\bBearer\s+[A-Za-z0-9._~+/=\-]{12,}"
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})"
)


def is_secret_key(name: Any) -> bool:
    """A field whose NAME says it holds a credential. `max_tokens` is not one."""
    lowered = str(name or "").strip().lower().replace("-", "_").replace(".", "_")
    if not lowered:
        return False
    if any(pair in lowered for pair in _SECRET_PAIRS):
        return True
    return any(part in _SECRET_WORDS for part in lowered.split("_"))


class _Redactor:
    def __init__(self) -> None:
        self.count = 0

    def text(self, value: str) -> str:
        def hide(_: re.Match[str]) -> str:
            self.count += 1
            return REDACTED
        return _SECRET_VALUE.sub(hide, value)

    def value(self, value: Any, key: Any = None) -> Any:
        if key is not None and is_secret_key(key) and value not in (None, "", [], {}):
            self.count += 1
            return REDACTED
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value


# ---------------------------------------------------------------------------------------------
# Typed previews
# ---------------------------------------------------------------------------------------------

Previewer = Callable[[dict[str, Any], "_Redactor", "_Lookup"], dict[str, Any]]


class _Lookup:
    """Read-only facts a preview may need that are not in the inputs (a media file's name)."""

    def __init__(self, connection: Any = None, project_id: Optional[str] = None) -> None:
        self.connection = connection
        self.project_id = project_id

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        if self.connection is None or not artifact_id:
            return {}
        try:
            from sqlalchemy import text

            row = self.connection.execute(
                text("SELECT title, uri, content FROM artifacts WHERE id=:i AND project_id=:p"),
                {"i": artifact_id, "p": self.project_id},
            ).mappings().first()
        except Exception:  # noqa: BLE001 — a preview never stops a gate opening
            return {}
        if not row:
            return {}
        content = row["content"] if isinstance(row["content"], dict) else {}
        return {"title": row["title"], "uri": row["uri"], "content": content}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + "…", True


def _recipients(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        items = [p.strip() for p in re.split(r"[,;]", str(value or ""))]
    return [i for i in items if i]


def _attachment_names(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("filename") or item.get("name") or item.get("title") \
                or item.get("path") or item.get("url") or item.get("id")
        else:
            name = item
        name = str(name or "").strip()
        if name:
            names.append(name.rsplit("/", 1)[-1] or name)
    return names


def preview_email(inputs: dict[str, Any], redact: _Redactor, _lookup: _Lookup) -> dict[str, Any]:
    body, truncated = _clip(redact.text(_text(inputs.get("body") or inputs.get("text"))),
                            MAX_BODY_CHARS)
    return {
        "kind": "email",
        "to": _recipients(inputs.get("to") or inputs.get("channel")),
        "cc": _recipients(inputs.get("cc")),
        "subject": redact.text(_text(inputs.get("subject"))).strip() or None,
        "body": body,
        "body_truncated": truncated,
        "attachments": _attachment_names(inputs.get("attachments")),
    }


def preview_issues(inputs: dict[str, Any], redact: _Redactor, _lookup: _Lookup) -> dict[str, Any]:
    actions = inputs.get("actions") or inputs.get("issues") or []
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list):
        actions = []
    items = []
    for action in actions[:MAX_ITEMS]:
        if not isinstance(action, dict):
            items.append({"title": redact.text(_text(action)), "owner": None,
                          "excerpt": "", "due": None})
            continue
        excerpt, _ = _clip(
            redact.text(_text(action.get("detail") or action.get("description")
                              or action.get("body"))).strip(), EXCERPT_CHARS)
        items.append({
            "title": redact.text(_text(action.get("title"))).strip() or "(no title)",
            "owner": (str(action.get("owner") or action.get("assignee") or "").strip()
                      or None),
            "excerpt": excerpt,
            "due": str(action.get("due") or "").strip() or None,
        })
    return {
        "kind": "issues",
        "items": items,
        "count": len(actions),
        "tracker": str(inputs.get("provider_key") or "").strip() or None,
    }


def preview_publish(inputs: dict[str, Any], redact: _Redactor, lookup: _Lookup,
                    *, platform: str = "YouTube") -> dict[str, Any]:
    media_id = str(inputs.get("media_artifact_id") or inputs.get("media_id") or "").strip()
    found = lookup.artifact(media_id) if media_id else {}
    content = found.get("content") or {}
    media_name = (content.get("filename") or content.get("name") or found.get("title")
                  or media_id or None)
    link = (content.get("web_view_link") or content.get("drive_url") or content.get("url")
            or inputs.get("video_url") or inputs.get("media_url") or None)
    description, truncated = _clip(redact.text(_text(inputs.get("description"))), MAX_BODY_CHARS)
    tags = inputs.get("tags") or []
    return {
        "kind": "publish",
        "platform": platform,
        "title": redact.text(_text(inputs.get("title"))).strip() or None,
        "description": description,
        "description_truncated": truncated,
        "visibility": str(inputs.get("visibility") or "private"),
        "tags": [str(t) for t in tags][:30] if isinstance(tags, list) else [],
        "media": {"id": media_id or None, "name": str(media_name) if media_name else None,
                  "link": str(link) if link else None},
    }


def _label_of(key: str) -> str:
    words = re.sub(r"[_\-.]+", " ", str(key)).strip()
    return (words[:1].upper() + words[1:]) if words else str(key)


def preview_fields(inputs: dict[str, Any], redact: _Redactor, _lookup: _Lookup) -> dict[str, Any]:
    fields = []
    for key, value in list((inputs or {}).items())[:MAX_ITEMS]:
        if is_secret_key(key) and value not in (None, "", [], {}):
            redact.count += 1
            fields.append({"name": key, "label": _label_of(key), "value": REDACTED,
                           "redacted": True})
            continue
        shown, truncated = _clip(_text(redact.value(value)), MAX_FIELD_CHARS)
        fields.append({"name": key, "label": _label_of(key), "value": shown,
                       "truncated": truncated})
    return {"kind": "fields", "fields": fields}


#: Skill id -> the preview that reads its inputs. Anything absent gets `preview_fields`.
PREVIEWERS: dict[str, Previewer] = {
    "email.send_real_guarded": preview_email,
    "email.send_real": preview_email,
    "tracker.issues.create": preview_issues,
    "publish_youtube": preview_publish,
}


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------


def build(
    skill_id: Optional[str],
    inputs: dict[str, Any],
    *,
    step_label: str = "",
    connection: Any = None,
    project_id: Optional[str] = None,
) -> dict[str, Any]:
    """The preview of what running `skill_id` with these exact `inputs` will do."""
    inputs = inputs if isinstance(inputs, dict) else {}
    redact = _Redactor()
    builder = PREVIEWERS.get(str(skill_id or ""), preview_fields)
    try:
        preview = builder(inputs, redact, _Lookup(connection, project_id))
    except Exception:  # noqa: BLE001 — a typed preview that cannot read its inputs falls back
        redact = _Redactor()
        preview = preview_fields(inputs, redact, _Lookup(connection, project_id))
    preview.update({
        "skill_id": skill_id or None,
        "step_label": step_label or None,
        "content_hash": content_hash(inputs),
        "redactions": redact.count,
    })
    return preview


def unresolved(skill_id: Optional[str], reason: str, *, step_label: str = "") -> dict[str, Any]:
    """A preview for a step whose inputs cannot be resolved yet — said, not left blank."""
    return {"kind": "unresolved", "skill_id": skill_id or None, "step_label": step_label or None,
            "reason": str(reason)[:500], "content_hash": None, "redactions": 0}


def run_title(run_input: Any, workflow_name: str = "") -> str:
    """The run in words: what it was started for, else the workflow's own name."""
    run_input = run_input if isinstance(run_input, dict) else {}
    for key in ("title", "name", "app_name", "subject"):
        value = run_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return (workflow_name or "").strip()[:80] or "this run"


def gate_question(step_label: str, title: str) -> str:
    """'Email the newsletter — E2E: Weekly newsletter'. No workflow keys, no run ids."""
    label = (step_label or "").strip() or "Approve this step"
    return f"{label} — {title}" if title else label


def stale_message(step_label: str, approved: str, current: str) -> str:
    return (
        f"What '{step_label or 'this step'}' would send changed after it was approved — "
        f"the approval covered content {approved.split(':')[-1][:10]}, and its inputs now "
        f"resolve to {current.split(':')[-1][:10]} (an earlier step was probably re-run). "
        "Nothing was sent. Retry this step to review the new content and approve it again."
    )
