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
pull_request  `data.changed_files` is fetched LAZILY (AMEX-G11, BOSSAPP-1169): `enrich` marks it
           pending, and the router asks `load_lazy` for it only when a route's filter reads it —
           GET /pulls/{n}/files, paginated, capped at MAX_CHANGED_FILES, with the connection's
           API token or the project's GitHub connection — so a route can filter
           `{"field": "data.changed_files", "op": "glob", "value": "tenants/*.yaml"}`.
poll       (AMEX-G1, BOSSAPP-1175) for an install GitHub cannot reach: `poll` lists the
           repository's open pull requests and branch heads since the cursor and turns each new
           head commit into a delivery IDENTICAL in shape to the webhook's (`pull_request`
           opened / synchronize, `push`), so parse, enrich, loop prevention and every route
           apply unchanged. `data.head_key` names the commit; the processor drops the second
           copy when the same commit arrived both by webhook and by poll.
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
    AuthFailed,
    Cursor,
    ExternalActor,
    IntakeConnection,
    IntakeEvent,
    ParseRefused,
    PersonHint,
    PollResult,
    RateLimited,
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
#: GitHub's page size for /pulls/{n}/files and the list endpoints.
PAGE_SIZE = 100
#: Pull requests and branches one poll reads at most (a repository with more open PRs than this
#: is past what polling is for: point a webhook at it).
MAX_POLLED_PULLS = 100
MAX_POLLED_BRANCHES = 300
#: `X-GitHub-Event` / `X-GitHub-Delivery` of a delivery the poller made, and how it is marked.
POLL_DELIVERY_PREFIX = "poll-"
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
    return resp.status, data, dict(resp.headers or {})


#: (method, url, headers, body, purpose) -> (status, parsed JSON or None[, answer headers]).
#: Tests replace it.
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

    def dedupe_key(self, conn: IntakeConnection, req: RawRequest) -> str:  # noqa: D102
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
        delivery = str(raw.headers.get(DELIVERY_HEADER) or "")
        data["delivered_by"] = "poll" if delivery.startswith(POLL_DELIVERY_PREFIX) else "webhook"
        head = _head_key(type_, data, full_name)
        if head:
            data["head_key"] = head
        ev = IntakeEvent(id=str(delivery or raw.dedupe_key.split(":", 1)[-1]),
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
        # A push the poller made carries no commits at all (`commits: null`): its list comes
        # from the compare, like a truncated payload's.
        complete = (isinstance(body.get("commits"), list) and len(commits) < PUSH_COMMIT_LIMIT
                    and all(
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
        """The changed files GitHub did not put in the payload, fetched once, through the
        outbound guard: a truncated push's compare, or a pull request's files. Failure leaves
        `changed_files_error` set; the event still routes."""
        if ev.type.startswith("com.github.pull_request.") and "changed_files" not in ev.data:
            # LAZY: fetched only when a route's filter reads the list (`load_lazy`).
            ev.data["changed_files"] = None
            ev.data["changed_files_source"] = "pending"
            return ev
        if ev.type != "com.github.push" or ev.data.get("changed_files") is not None:
            return ev
        repo, before, after = ev.data.get("repository"), ev.data.get("before"), ev.data.get("after")
        if (not repo or not _FULL_NAME.match(str(repo)) or not before or not after
                or not _SHA.match(str(before)) or not _SHA.match(str(after))
                or str(before).strip("0") == ""):
            ev.data["changed_files_source"] = "unavailable"
            return ev
        headers = _headers(conn, str(repo))
        url = f"{API_BASE}/repos/{repo}/compare/{quote(str(before), safe='')}...{quote(str(after), safe='')}"
        try:
            status, payload, _ = _transport_with_headers("GET", url, headers,
                                                         "intake.github.compare")
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

    #: Envelope fields this adapter fills only when a route's filter reads them (the router
    #: calls `load_lazy`): a pull request's file list costs GitHub calls nobody may need.
    lazy_fields = ("data.changed_files",)

    def load_lazy(self, conn: IntakeConnection, ev: IntakeEvent, field: str) -> IntakeEvent:
        if (field == "data.changed_files" and ev.type.startswith("com.github.pull_request.")
                and ev.data.get("changed_files_source") == "pending"):
            return self._pull_request_files(conn, ev)
        return ev

    def _pull_request_files(self, conn: IntakeConnection, ev: IntakeEvent) -> IntakeEvent:
        """`GET /repos/{repo}/pulls/{n}/files`, page by page, at most MAX_CHANGED_FILES files.
        Same shape as a push's list: [{filename, status, additions, deletions}] (a renamed
        file also carries `previous_filename`)."""
        repo, number = ev.data.get("repository"), str(ev.data.get("record_id") or "")
        ev.data["changed_files"] = None
        if not repo or not _FULL_NAME.match(str(repo)) or not number.isdigit():
            ev.data["changed_files_source"] = "unavailable"
            return ev
        headers = _headers(conn, str(repo))
        files: list[dict[str, Any]] = []
        page = 1
        while len(files) < MAX_CHANGED_FILES:
            url = (f"{API_BASE}/repos/{repo}/pulls/{number}/files"
                   f"?per_page={PAGE_SIZE}&page={page}")
            try:
                status, payload, _ = _transport_with_headers("GET", url, headers,
                                                             "intake.github.pr_files")
            except Exception as error:  # noqa: BLE001 — the event routes without the list
                ev.data["changed_files_error"] = f"{type(error).__name__}"
                ev.data["changed_files_source"] = "error"
                return ev
            if status != 200 or not isinstance(payload, list):
                ev.data["changed_files_error"] = f"GitHub answered {status}"
                ev.data["changed_files_source"] = "error"
                return ev
            for f in payload:
                if not isinstance(f, dict):
                    continue
                item = {"filename": str(f.get("filename") or ""),
                        "status": str(f.get("status") or "modified"),
                        "additions": int(f.get("additions") or 0),
                        "deletions": int(f.get("deletions") or 0)}
                if f.get("previous_filename"):
                    item["previous_filename"] = str(f["previous_filename"])
                files.append(item)
            if len(payload) < PAGE_SIZE:
                break
            page += 1
        ev.data["changed_files"] = files[:MAX_CHANGED_FILES]
        ev.data["changed_files_truncated"] = len(files) >= MAX_CHANGED_FILES
        ev.data["changed_files_source"] = "pulls_api"
        return ev

    # -- PULL (AMEX-G1) ---------------------------------------------------------------------------

    def poll(self, conn: IntakeConnection, cursor: Cursor, limit: int) -> PollResult:
        """New head commits since `cursor`, as webhook-shaped deliveries.

        THE CURSOR is JSON: {"pulls": {number: head_sha}, "branches": {name: sha},
        "etags": {...}, "since": ISO}. The first poll is a BASELINE: it records every head and
        emits only pull requests updated after the connection began polling (`since`), so
        connecting a busy repository does not replay its history. After that, a pull request
        new to the cursor is `opened`, a moved head is `synchronize`, and a branch whose head
        moved is a `push` (`before` = the previous head, so `enrich` fetches its compare).

        Requests carry `If-None-Match`; GitHub does not count a 304 against the rate limit.
        A 401/403 raises AuthFailed, a 403/429 with a reset raises RateLimited — the poller
        backs off and records it on the connection's health."""
        repo = str(conn.get("repository") or "").strip()
        if not _FULL_NAME.match(repo):
            raise ParseRefused("a polled GitHub connection names its repository (owner/name)",
                               provider=self.key)
        state = _loads_cursor(cursor.value)
        first = not state.get("polled")
        since = str(state.get("since") or cursor.cursor_time or "")
        etags: dict[str, str] = dict(state.get("etags") or {})
        headers = _headers(conn, repo)
        deliveries: list[StoredRaw] = []

        pulls_url = (f"{API_BASE}/repos/{repo}/pulls?state=open&sort=updated&direction=desc"
                     f"&per_page={min(PAGE_SIZE, MAX_POLLED_PULLS)}")
        status, pulls = self._get(pulls_url, headers, etags, "pulls")
        heads: dict[str, str] = dict(state.get("pulls") or {})
        if status == 200:
            seen: dict[str, str] = {}
            for pr in (pulls or [])[:MAX_POLLED_PULLS]:
                if not isinstance(pr, dict) or not isinstance(pr.get("head"), dict):
                    continue
                number, sha = str(pr.get("number") or ""), str(pr["head"].get("sha") or "")
                if not number.isdigit() or not _SHA.match(sha):
                    continue
                seen[number] = sha
                known = heads.get(number)
                if known == sha:
                    continue
                if first and not _later(pr.get("updated_at"), since):
                    continue                    # the baseline: already there when polling began
                action = "synchronize" if known else "opened"
                deliveries.append(_delivery(repo, "pull_request", {
                    "action": action, "number": int(number), "pull_request": pr,
                    "repository": (pr.get("base") or {}).get("repo") or {"full_name": repo},
                    "sender": pr.get("user") or {}}, f"pr:{repo}#{number}@{sha}"))
            heads = seen                         # a closed PR leaves the cursor
        branches_url = f"{API_BASE}/repos/{repo}/branches?per_page={PAGE_SIZE}"
        status_b, listed = self._get(branches_url, headers, etags, "branches")
        tips: dict[str, str] = dict(state.get("branches") or {})
        pushes = str(conn.get("poll_pushes", "true")).strip().lower() not in (
            "false", "0", "no", "off")
        if status_b == 200 and pushes:
            now_tips: dict[str, str] = {}
            for b in (listed or [])[:MAX_POLLED_BRANCHES]:
                if not isinstance(b, dict) or not isinstance(b.get("commit"), dict):
                    continue
                name, sha = str(b.get("name") or ""), str(b["commit"].get("sha") or "")
                if not name or not _SHA.match(sha):
                    continue
                now_tips[name] = sha
                before = tips.get(name)
                if first or before == sha:
                    continue
                deliveries.append(_delivery(repo, "push", {
                    "ref": f"refs/heads/{name}", "before": before or "0" * 40, "after": sha,
                    "created": before is None, "deleted": False, "forced": False,
                    "commits": None, "head_commit": None,
                    "repository": {"full_name": repo}, "sender": {}},
                    f"push:{repo}@refs/heads/{name}@{sha}"))
            tips = now_tips
        # Every new head is returned, never a page of them: the cursor moves past all of them
        # in the same transaction as their inserts, so a cut here would lose the rest.
        next_state = {"polled": True, "since": since, "pulls": heads, "branches": tips,
                      "etags": etags}
        events = tuple(ev for raw in deliveries for ev in self.parse(conn, raw))
        return PollResult(events=events, deliveries=tuple(deliveries),
                          next_cursor=Cursor(stream_key=cursor.stream_key,
                                             value=json.dumps(next_state, sort_keys=True),
                                             cursor_time=since or None))

    def _get(self, url: str, headers: Mapping[str, str], etags: dict[str, str],
             name: str) -> tuple[int, Any]:
        """One conditional GET: 304 → (304, None) and nothing changed."""
        sent = dict(headers)
        if etags.get(name):
            sent["If-None-Match"] = etags[name]
        status, payload, answer_headers = _transport_with_headers("GET", url, sent,
                                                                  f"intake.github.poll.{name}")
        if status in (401,):
            raise AuthFailed("GitHub refused the connection's token (401)", provider=self.key)
        if status in (403, 429):
            remaining = str((answer_headers or {}).get("x-ratelimit-remaining") or "")
            retry = (answer_headers or {}).get("retry-after")
            if status == 429 or remaining == "0" or retry:
                try:
                    wait = float(retry) if retry else None
                except ValueError:
                    wait = None
                raise RateLimited(f"GitHub rate limit ({status})", retry_after=wait,
                                  provider=self.key)
            raise AuthFailed(f"GitHub refused the poll ({status})", provider=self.key)
        if status == 200 and (answer_headers or {}).get("etag"):
            etags[name] = str(answer_headers["etag"])
        if status not in (200, 304):
            raise RuntimeError(f"GitHub answered {status} for {name}")
        return status, payload

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


def _headers(conn: IntakeConnection, repo: str) -> dict[str, str]:
    """GitHub request headers with the connection's API token, or else the project's own
    GitHub connection for that repository (github_connections.github_token_for). Never handed
    to an agent, never logged."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "boss-intake"}
    token = conn.secret("api_token")
    if not token and conn.db is not None and conn.project_id:
        try:
            from ....services.github_connections import github_token_for

            owner, _, name = repo.partition("/")
            settings = conn.app_settings
            if settings is None:
                from ....config import get_settings

                settings = get_settings()
            with conn.db.begin_nested():
                token = github_token_for(conn.db, settings, conn.project_id, owner, name)
        except Exception:  # noqa: BLE001 — no token: a public repository still answers
            token = ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _transport_with_headers(method: str, url: str, headers: Mapping[str, str],
                            purpose: str) -> tuple[int, Any, dict[str, str]]:
    """TRANSPORT, with the answer's headers when the transport gives them (ETag, rate limit).
    A transport may return (status, data) or (status, data, headers)."""
    got = TRANSPORT(method, url, headers, None, purpose)
    if isinstance(got, tuple) and len(got) == 3:
        status, data, answer = got
        return int(status), data, {str(k).lower(): str(v) for k, v in dict(answer or {}).items()}
    status, data = got
    return int(status), data, {}


def _later(value: Any, since: str) -> bool:
    """Whether GitHub's `updated_at` is after `since` (both ISO-8601; compared as instants)."""
    from datetime import datetime, timezone

    def instant(text: Any) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    left, right = instant(value), instant(since)
    return left is not None and right is not None and left > right


def _loads_cursor(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _delivery(repo: str, event: str, body: dict[str, Any], key: str) -> StoredRaw:
    """A polled change as the webhook would have delivered it."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return StoredRaw(body=json.dumps(body, sort_keys=True, default=str).encode("utf-8"),
                     headers={EVENT_HEADER: event, DELIVERY_HEADER: POLL_DELIVERY_PREFIX + digest},
                     dedupe_key=f"github:poll:{key}"[:500])


def _head_key(type_: str, data: Mapping[str, Any], repo: str) -> Optional[str]:
    """The commit an event is about, the same whether it came by webhook or by poll: a PR
    that opened or moved to a head, or a branch that moved to a commit."""
    if type_ in ("com.github.pull_request.opened", "com.github.pull_request.synchronize"):
        sha, number = str(data.get("head_sha") or ""), str(data.get("record_id") or "")
        return f"pr:{repo}#{number}@{sha}" if repo and number and sha else None
    if type_ == "com.github.push" and not data.get("deleted"):
        ref, after = str(data.get("ref") or ""), str(data.get("after") or "")
        return f"push:{repo}@{ref}@{after}" if repo and ref and after else None
    return None


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
