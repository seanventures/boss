"""GitHub Issues behind the ticket contract: the existing create/test, plus read (one issue and
its comments) and comment. Browse, transition, link and webhooks are not built here yet (the
manifest says so; they raise CapabilityNotSupported)."""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import quote, urlsplit

from ....services.work_tracker_adapters import GitHubIssuesWorkTrackerAdapter
from ..base import TicketAdapterBase, normalise_type
from ..contract import (
    NotFound,
    Ticket,
    TicketComment,
    TicketConnection,
    TicketRef,
)
from ..text import acceptance_criteria

_ISSUE_URL = re.compile(r"^/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)/issues/(\d+)/?$")
_SHORT = re.compile(r"(?<![\w/.-])([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)#(\d+)\b")
_LABEL_TYPES = {"bug": "bug", "enhancement": "story", "feature": "story", "epic": "epic",
                "task": "task", "story": "story"}


class GitHubIssuesTicketAdapter(GitHubIssuesWorkTrackerAdapter, TicketAdapterBase):
    def auth_headers(self, conn: TicketConnection, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}

    def _repo(self, conn: Optional[TicketConnection]) -> tuple[str, str]:
        if conn is None:
            return "", ""
        return str(conn.get("owner") or "").strip(), str(conn.get("repo") or "").strip()

    def ref_from_url(self, url: str, conn: Optional[TicketConnection] = None) -> Optional[TicketRef]:
        parts = urlsplit(url)
        if (parts.hostname or "").lower() != "github.com":
            return None
        match = _ISSUE_URL.match(parts.path)
        if not match:
            return None
        owner, repo, number = match.groups()
        return TicketRef(self.provider_key, key=f"{owner}/{repo}#{number}", url=url,
                         project=f"{owner}/{repo}")

    def parse_ref(self, text: str, *, conn: Optional[TicketConnection] = None,
                  known_keys: Optional[Iterable[str]] = None) -> list[TicketRef]:
        """URLs, plus ``owner/repo#N`` for the connection's own repository only."""
        refs = super().parse_ref(text, conn=conn, known_keys=known_keys)
        owner, repo = self._repo(conn)
        if owner and repo:
            for match in _SHORT.finditer(text or ""):
                if (match.group(1).lower(), match.group(2).lower()) == (owner.lower(), repo.lower()):
                    key = f"{match.group(1)}/{match.group(2)}#{match.group(3)}"
                    if not any(r.key and r.key.lower() == key.lower() for r in refs):
                        refs.append(TicketRef(self.provider_key, key=key,
                                              project=f"{match.group(1)}/{match.group(2)}"))
        return refs

    def _locate(self, conn: TicketConnection, ref: TicketRef) -> tuple[str, str, str]:
        owner, repo = self._repo(conn)
        text = str(ref.key or ref.id or "")
        match = re.match(r"^([^/\s]+)/([^#\s]+)#(\d+)$", text)
        if match:
            return match.group(1), match.group(2), match.group(3)
        number = text.lstrip("#")
        if number.isdigit() and owner and repo:
            return owner, repo, number
        if ref.url:
            found = self.ref_from_url(ref.url)
            if found is not None:
                return self._locate(conn, found)
        raise NotFound("github_issues: which issue? give owner/repo#N, a number, or its URL",
                       provider=self.provider_key)

    def fetch_ticket(self, conn: TicketConnection, ref: TicketRef) -> Ticket:
        http = self.session(conn)
        owner, repo, number = self._locate(conn, ref)
        root = f"/repos/{quote(owner)}/{quote(repo)}/issues/{number}"
        issue = http.get_json(root) or {}
        limit = int(self.settings(conn).get("comments_limit") or 0)
        comments = []
        if limit and issue.get("comments"):
            rows = http.get_json(f"{root}/comments", params={"per_page": 100}) or []
            comments = [TicketComment((r.get("user") or {}).get("login"), r.get("created_at"),
                                      str(r.get("body") or "")) for r in rows[-limit:]]
        labels = [str(lb.get("name") if isinstance(lb, dict) else lb) for lb in issue.get("labels") or []]
        issue_type = (issue.get("type") or {}).get("name") if isinstance(issue.get("type"), dict) \
            else None
        kind = normalise_type(issue_type) if issue_type else next(
            (_LABEL_TYPES[lb.lower()] for lb in labels if lb.lower() in _LABEL_TYPES), "task")
        body = str(issue.get("body") or "")
        summary = issue.get("sub_issues_summary") or {}
        state = str(issue.get("state") or "open")
        return Ticket(
            provider=self.provider_key, key=f"{owner}/{repo}#{number}",
            id=str(issue.get("id") or number), url=issue.get("html_url"),
            title=str(issue.get("title") or ""), body_markdown=body, type=kind,
            raw_type=issue_type, status=state,
            status_category=self.category_for(conn, state, "done" if state == "closed" else "todo"),
            labels=labels, assignee=(issue.get("assignee") or {}).get("login"),
            reporter=(issue.get("user") or {}).get("login"),
            acceptance_criteria=acceptance_criteria(body), comments=comments,
            children_count=summary.get("total") if isinstance(summary, dict) else None,
            updated_at=issue.get("updated_at"))

    def post_comment(self, conn: TicketConnection, ref: TicketRef, markdown: str) -> TicketComment:
        owner, repo, number = self._locate(conn, ref)
        row = self.session(conn).post_json(
            f"/repos/{quote(owner)}/{quote(repo)}/issues/{number}/comments", {"body": markdown}) or {}
        return TicketComment((row.get("user") or {}).get("login"), row.get("created_at"),
                             str(row.get("body") or markdown))

    def known_keys(self, conn: Optional[TicketConnection]) -> set[str]:
        return set()

