"""GitHub on the intake layer (P9a). Lives BESIDE `/v1/webhooks/github` (services/webhooks.py);
the build workflows keep that route until P9b migrates them.

verify     X-Hub-Signature-256: sha256=<hex HMAC-SHA256(secret, raw body)> (constant time)
dedupe     X-GitHub-Delivery (GitHub re-delivers with the same id; a missing id falls back to
           the body hash so nothing is lost)
handshake  the `ping` event answers 200 and stores nothing
types      com.github.<event>.<action> (`com.github.pull_request.opened`), or com.github.<event>
           when the event has no action (`com.github.push`)
self       sender.login == our bot login (connection `bot_login`, else the install's GitHub App
           bot) or sender.type == "Bot" unless allow-listed — loop prevention, layer 1
push       `data.changed_files` is built from the commits in the payload when GitHub sent them
           all; a truncated payload (> 20 commits) fetches the compare lazily in `enrich`, in
           the processor, never in the request — and only when the connection holds a token.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from ..base import IntakeAdapterBase
from ..contract import (
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    ParseRefused,
    PersonHint,
    RawRequest,
    RawResponse,
    StoredRaw,
    Verified,
    VerifyRefused,
)

SIGNATURE_HEADER = "x-hub-signature-256"
DELIVERY_HEADER = "x-github-delivery"
EVENT_HEADER = "x-github-event"
API_BASE = "https://api.github.com"
#: GitHub puts at most this many commits in a push payload; beyond it the list is truncated.
PUSH_COMMIT_LIMIT = 20
MAX_CHANGED_FILES = 300
#: `owner/name` as GitHub spells it; anything else never reaches the compare URL.
_FULL_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_FIELD_WHITELIST = {
    "issues": ("number", "title", "state", "html_url", "labels", "assignees", "milestone",
               "created_at", "updated_at", "closed_at", "author_association", "draft"),
    "pull_request": ("number", "title", "state", "html_url", "labels", "assignees", "draft",
                     "merged", "mergeable_state", "base", "head", "created_at", "updated_at",
                     "merged_at", "closed_at", "requested_reviewers", "author_association"),
    "release": ("tag_name", "name", "draft", "prerelease", "html_url", "published_at"),
    "workflow_run": ("name", "status", "conclusion", "html_url", "run_number", "head_branch",
                     "head_sha", "event"),
    "check_suite": ("status", "conclusion", "head_branch", "head_sha", "app"),
}


def github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _default_transport(method: str, url: str, headers: Mapping[str, str],
                       body: Optional[bytes], purpose: str) -> tuple[int, Any]:
    from ....identity.http import fetch

    resp = fetch(method, url, headers=dict(headers), data=body, purpose=purpose)
    try:
        data = resp.json()
    except ValueError:
        data = None
    return resp.status, data


#: (method, url, headers, body, purpose) -> (status, parsed JSON or None). Tests replace it.
TRANSPORT: Callable[..., tuple[int, Any]] = _default_transport


def _login(obj: Any) -> str:
    return str((obj or {}).get("login") or "") if isinstance(obj, dict) else ""


def _csv(value: Any) -> set[str]:
    return {p.strip().casefold() for p in str(value or "").split(",") if p.strip()}


class GitHubIntakeAdapter(IntakeAdapterBase):
    key = "github"

    # -- PUSH -------------------------------------------------------------------------------------

    def verify(self, conn: IntakeConnection, req: RawRequest, now: float) -> Verified:
        given = req.header(SIGNATURE_HEADER).strip()
        if not given:
            raise VerifyRefused("missing_signature", provider=self.key)
        secret = conn.secret("webhook_secret") or ""
        if not secret:
            raise VerifyRefused("not_configured", provider=self.key)
        if not hmac.compare_digest(given, github_signature(secret, req.body)):
            raise VerifyRefused("bad_signature", provider=self.key)
        return Verified(scheme="github_sha256")

    def handshake(self, conn: IntakeConnection, req: RawRequest) -> Optional[RawResponse]:
        if req.header(EVENT_HEADER).strip().lower() == "ping":
            return RawResponse(status=200, body={"pong": True})
        return None

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:
        delivery = req.header(DELIVERY_HEADER).strip()[:200]
        if delivery:
            return f"github:{delivery}"
        return "github:sha256:" + req.body_sha256()

    # -- PROCESSOR --------------------------------------------------------------------------------

    def parse(self, conn: IntakeConnection, raw: StoredRaw) -> list[IntakeEvent]:
        try:
            body = json.loads(raw.body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError) as error:
            raise ParseRefused(f"GitHub body is not JSON: {type(error).__name__}",
                               provider=self.key) from None
        if not isinstance(body, dict):
            raise ParseRefused("GitHub body is not an object", provider=self.key)
        event = str(raw.headers.get(EVENT_HEADER) or "").strip().lower()
        if not event:
            raise ParseRefused("no X-GitHub-Event header", provider=self.key)
        repo = body.get("repository") if isinstance(body.get("repository"), dict) else {}
        full_name = str(repo.get("full_name") or "")
        wanted = str(conn.get("repository") or "").strip().casefold()
        if wanted and full_name.casefold() != wanted:
            return []       # another repository on a shared hook: ignored, not an error
        action = str(body.get("action") or "").strip().lower()
        type_ = f"com.github.{event}" + (f".{action}" if action else "")
        installation = (body.get("installation") or {}).get("id") if isinstance(
            body.get("installation"), dict) else None
        source = f"github://{installation}" if installation else f"github://{full_name or 'unknown'}"
        sender = body.get("sender") if isinstance(body.get("sender"), dict) else {}
        actor = ExternalActor(
            kind="bot" if str(sender.get("type") or "").lower() == "bot" else
            ("user" if sender else "unknown"),
            external_id=str(sender.get("id") or ""), display=_login(sender))
        data: dict[str, Any] = {"event": event, "action": action or None,
                                "repository": full_name or None,
                                "repository_private": bool(repo.get("private"))}
        subject: Optional[str] = full_name or None
        time_ = None
        if event == "push":
            subject, time_ = self._push(body, data, full_name)
        elif event in ("issues", "issue_comment") and isinstance(body.get("issue"), dict):
            issue = body["issue"]
            number = issue.get("number")
            is_pr = isinstance(issue.get("pull_request"), dict)
            subject = f"{full_name}#{number}"
            data.update(record_type="pull_request" if is_pr else "issue", record_id=str(number),
                        record_url=issue.get("html_url"),
                        operation=_operation(event, action),
                        fields=_fields(issue, "issues"),
                        title=str(issue.get("title") or ""),
                        body=str(issue.get("body") or "")[:20000] if event == "issues" else "",
                        changed_fields=_changed(body))
            if event == "issue_comment" and isinstance(body.get("comment"), dict):
                data["comment"] = str(body["comment"].get("body") or "")[:20000]
                data["comment_url"] = body["comment"].get("html_url")
            time_ = (body.get("comment") or issue).get("updated_at") if isinstance(
                body.get("comment") or issue, dict) else None
        elif event.startswith("pull_request") and isinstance(body.get("pull_request"), dict):
            pr = body["pull_request"]
            number = pr.get("number")
            subject = f"{full_name}#{number}"
            data.update(record_type="pull_request", record_id=str(number),
                        record_url=pr.get("html_url"), operation=_operation(event, action),
                        fields=_fields(pr, "pull_request"), title=str(pr.get("title") or ""),
                        body=str(pr.get("body") or "")[:20000] if event == "pull_request" else "",
                        changed_fields=_changed(body),
                        base_ref=((pr.get("base") or {}).get("ref")),
                        head_ref=((pr.get("head") or {}).get("ref")),
                        head_sha=((pr.get("head") or {}).get("sha")))
            for key in ("review", "comment"):
                if isinstance(body.get(key), dict):
                    data[key] = str(body[key].get("body") or "")[:20000]
                    data[f"{key}_state"] = body[key].get("state")
            time_ = pr.get("updated_at")
        else:
            record = next((body[k] for k in ("release", "workflow_run", "check_suite", "discussion",
                                             "deployment") if isinstance(body.get(k), dict)), None)
            if record is not None:
                data.update(record_type=event, record_id=str(record.get("id") or ""),
                            record_url=record.get("html_url"),
                            operation=_operation(event, action),
                            fields=_fields(record, event), changed_fields=_changed(body))
                subject = f"{full_name}/{event}/{record.get('id')}" if full_name else None
                time_ = record.get("updated_at") or record.get("created_at")
            else:
                data.update(record_type=event, operation=_operation(event, action),
                            fields={k: v for k, v in body.items()
                                    if isinstance(v, (str, int, float, bool)) and k != "action"})
        ev = IntakeEvent(id=str(raw.headers.get(DELIVERY_HEADER) or raw.dedupe_key.split(":", 1)[-1]),
                         source=source, type=type_, subject=subject, time=time_, data=data,
                         bossactor=actor, bossconnection=conn.id, bossdedupe=raw.dedupe_key)
        if isinstance(body.get("boss"), dict):
            ev.bosscausality = {"run_id": body["boss"].get("run_id"),
                                "deployment_id": body["boss"].get("deployment_id"),
                                "hops": int(body["boss"].get("hops") or 0)}
        return [ev]

    def _push(self, body: dict, data: dict, full_name: str) -> tuple[Optional[str], Optional[str]]:
        ref = str(body.get("ref") or "")
        commits = body.get("commits") if isinstance(body.get("commits"), list) else []
        data.update(record_type="push", record_id=ref, operation="created",
                    ref=ref, before=body.get("before"), after=body.get("after"),
                    created=bool(body.get("created")), deleted=bool(body.get("deleted")),
                    forced=bool(body.get("forced")), compare_url=body.get("compare"),
                    commit_count=len(commits),
                    pusher=(body.get("pusher") or {}).get("name") if isinstance(
                        body.get("pusher"), dict) else None,
                    commits=[{"id": c.get("id"), "message": str(c.get("message") or "")[:2000],
                              "author": (c.get("author") or {}).get("username")
                              or (c.get("author") or {}).get("name"),
                              "timestamp": c.get("timestamp")} for c in commits[:PUSH_COMMIT_LIMIT]
                             if isinstance(c, dict)])
        head = body.get("head_commit") if isinstance(body.get("head_commit"), dict) else None
        data["message"] = str((head or {}).get("message") or "")[:2000]
        complete = (len(commits) < PUSH_COMMIT_LIMIT and all(
            isinstance(c, dict) and all(isinstance(c.get(k), list)
                                        for k in ("added", "removed", "modified"))
            for c in commits))
        if complete:
            data["changed_files"] = _files_from_commits(commits)
            data["changed_files_source"] = "payload"
        else:
            data["changed_files"] = None       # lazily fetched by `enrich`
            data["changed_files_source"] = "pending"
        subject = f"{full_name}@{ref}" if full_name else ref
        return subject, (head or {}).get("timestamp") if head else None

    def enrich(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent:
        """A truncated push payload: fetch `before...after` once, through the outbound guard,
        with the connection's optional API token. Failure leaves `changed_files_error` set; the
        event still routes."""
        if ev.type != "com.github.push" or ev.data.get("changed_files") is not None:
            return ev
        repo, before, after = ev.data.get("repository"), ev.data.get("before"), ev.data.get("after")
        if (not repo or not _FULL_NAME.match(str(repo)) or not before or not after
                or not _SHA.match(str(before)) or not _SHA.match(str(after))
                or str(before).strip("0") == ""):
            ev.data["changed_files_source"] = "unavailable"
            return ev
        token = conn.secret("api_token")
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "boss-intake"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{API_BASE}/repos/{repo}/compare/{quote(str(before), safe='')}...{quote(str(after), safe='')}"
        try:
            status, payload = TRANSPORT("GET", url, headers, None, "intake.github.compare")
        except Exception as error:  # noqa: BLE001 — the event routes without the list
            ev.data["changed_files_error"] = f"{type(error).__name__}"
            ev.data["changed_files_source"] = "error"
            return ev
        if status != 200 or not isinstance(payload, dict):
            ev.data["changed_files_error"] = f"GitHub answered {status}"
            ev.data["changed_files_source"] = "error"
            return ev
        ev.data["changed_files"] = [
            {"filename": str(f.get("filename") or ""), "status": str(f.get("status") or "modified"),
             "additions": int(f.get("additions") or 0), "deletions": int(f.get("deletions") or 0)}
            for f in (payload.get("files") or [])[:MAX_CHANGED_FILES] if isinstance(f, dict)]
        ev.data["changed_files_source"] = "compare_api"
        return ev

    def is_self(self, conn: IntakeConnection, ev: IntakeEvent) -> bool:
        login = ev.bossactor.display.casefold()
        ours = str(conn.get("bot_login") or "").strip().casefold()
        if not ours:
            try:
                from ....config import get_settings

                ours = str(get_settings().github_app_bot_login or "").strip().casefold()
            except Exception:  # noqa: BLE001 — no settings: only the connection's own name
                ours = ""
        if login and ours and login == ours:
            return True
        if ev.bossactor.kind == "bot" and login not in _csv(conn.get("allow_bots")):
            return True
        return False

    def resolve_person(self, conn: IntakeConnection, actor: ExternalActor) -> PersonHint:
        return PersonHint(email=actor.email, display=actor.display, is_bot=actor.kind == "bot")

    def conversation_key(self, conn: IntakeConnection, ev: IntakeEvent) -> Optional[str]:
        return ev.subject


def _operation(event: str, action: str) -> str:
    if event in ("issue_comment", "pull_request_review_comment", "pull_request_review",
                 "discussion_comment", "commit_comment"):
        return "deleted" if action == "deleted" else "commented"
    if event == "push" or action in ("opened", "created", "published", "requested"):
        return "created"
    if action in ("deleted", "removed"):
        return "deleted"
    return "updated"


def _changed(body: dict) -> list[str]:
    changes = body.get("changes")
    return sorted(str(k) for k in changes) if isinstance(changes, dict) else []


def _fields(record: dict, kind: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _FIELD_WHITELIST.get(kind, ()):
        value = record.get(name)
        if value is None:
            continue
        if name in ("labels", "assignees", "requested_reviewers") and isinstance(value, list):
            out[name] = [str(v.get("name") or v.get("login") or "") if isinstance(v, dict)
                         else str(v) for v in value][:50]
        elif name in ("base", "head") and isinstance(value, dict):
            out[name] = {"ref": value.get("ref"), "sha": value.get("sha")}
        elif name in ("milestone", "app") and isinstance(value, dict):
            out[name] = value.get("title") or value.get("name") or value.get("slug")
        elif isinstance(value, (str, int, float, bool)):
            out[name] = value
    return out


def _files_from_commits(commits: list) -> list[dict[str, Any]]:
    seen: dict[str, str] = {}
    for commit in commits:
        for status, key in (("added", "added"), ("removed", "removed"), ("modified", "modified")):
            for name in commit.get(key) or []:
                seen[str(name)] = status
    return [{"filename": name, "status": status}
            for name, status in list(seen.items())[:MAX_CHANGED_FILES]]


__all__ = ["GitHubIntakeAdapter", "TRANSPORT", "github_signature"]
