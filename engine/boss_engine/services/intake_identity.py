"""WHO AN INTAKE RUN ACTS FOR (P9a-2, BOSSAPP-1094; boss-eventing-lld.md §6).

A run has a SUBJECT (the person it acts for) and an ACTOR (the agent). The route's
`identity_mode` decides the subject per event:

  person            the linked human who sent the message; no link → NO run, one link prompt
                    per user per hour (chat conversations; the default for Slack/Teams)
  person_or_owner   the linked human if any, else the route owner's standing grant (record
                    events people cause: GitHub/Jira comments)
  owner             the route owner's standing grant, re-checked at every firing by
                    agent_triggers.fire (service events: alerts, generic webhooks — the ONLY
                    mode a generic webhook may use: `subject_hint` is data, never identity)
  (owner_readonly — the owner's grant with write tools dropped — is NOT offered until the
   kernel filters tools by it; a label alone would run unknown senders with full write rights)

HOW A PERSON IS FOUND, per source (§6.2):
  * a chat source (the connection owns a chat endpoint): a verified `chat_identity_links` row
    for (endpoint, external user), re-checked at every use (not revoked; principal still active);
  * SSO attestation, opt-in per connection (`config.sso_attested = {idp, attested_by, attested_at,
    domains:[...]}`): the actor's email → identity/users.by_email, active, email verified, domain
    attested → a `method='sso_email'` link is written. Guests and external users are NEVER
    auto-linked (Slack is_restricted / is_ultra_restricted / another team; Teams guest userType /
    another tenant) — an email in a guest's profile proves nothing;
  * GitHub: the sender's numeric id against the person's own GitHub sign-in
    (identity_user_identities, issuer github); `sender.type == 'Bot'` is never a person;
  * generic webhook: nobody. Owner only — `allowed_modes("webhook")` refuses the person modes
    when a route is saved, so a sender's `actor` is never more than data.

THE PERSON'S AUTHORITY: a standing grant of theirs that covers this agent in this project
(identity/grants.find_covering). With none, the agent does not run for them: they get one
private message with the console path to grant it, and the event waits `GRANT_WAIT` and fires
when the grant lands (the route's deferral). Under shadow/off authz the run still names the
person as its subject (audit) without a grant, as every other door does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.intake.contract import IntakeConnection, IntakeEvent
from .core import emit_event, make_id

logger = logging.getLogger("boss.intake.identity")

MODES = ("person", "person_or_owner", "owner")
#: Sources whose events are conversations with a person (default identity mode `person`).
CHAT_KINDS = ("slack", "teams")
#: Sources whose events people cause but that are not conversations.
RECORD_PERSON_KINDS = ("github", "jira", "plane", "servicenow", "salesforce", "gmail", "outlook")
MAIL_KINDS = ("gmail", "outlook")
LINK_METHODS = ("code", "sso_email", "admin", "aad_oid", "dmarc_email")
#: An event held for a person who has no grant yet, then fired when it lands (§6.1).
GRANT_WAIT = timedelta(minutes=30)
#: The link prompt (and the grant prompt) go out at most once per user per hour.
PROMPT_BUCKET_SECONDS = 3600
GRANT_PATH = "/account/agents"


@dataclass
class Subject:
    mode: str
    kind: str = "none"                       # person | owner | none
    principal_id: Optional[str] = None
    acting: Any = None                       # identity.lineage.Acting, or None (owner path)
    link_id: Optional[str] = None
    method: Optional[str] = None
    external_id: str = ""
    display: str = ""
    reason: str = ""
    prompted: bool = False
    read_only: bool = False
    deferred_until: Optional[datetime] = None

    @property
    def runs(self) -> bool:
        return self.kind in ("person", "owner") and self.deferred_until is None

    def audit(self) -> dict[str, Any]:
        return {"mode": self.mode, "kind": self.kind, "principal_id": self.principal_id,
                "link_id": self.link_id, "method": self.method, "external_id": self.external_id,
                "display": self.display, "reason": self.reason, "read_only": self.read_only}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_mode(kind: str) -> str:
    if kind in CHAT_KINDS:
        return "person"
    if kind in RECORD_PERSON_KINDS:
        return "person_or_owner"
    return "owner"


def allowed_modes(kind: str) -> tuple[str, ...]:
    """What a route on a source of `kind` may ask for. A generic webhook carries no identity, so
    only the owner modes (§6.2 'Generic webhook: owner only')."""
    if kind == "webhook":
        return ("owner",)
    return MODES


def is_guest(conn: IntakeConnection, event: IntakeEvent) -> bool:
    """Slack guests / Slack Connect users, Teams guests / other tenants: never auto-linked."""
    data = event.data or {}
    for key in ("is_restricted", "is_ultra_restricted", "user_is_restricted", "is_stranger",
                "is_external", "is_guest"):
        if data.get(key) is True:
            return True
    if str(data.get("user_type") or data.get("userType") or "").lower() == "guest":
        return True
    team = data.get("user_team_id") or data.get("team_id")
    own_team = conn.get("team_id")
    if team and own_team and str(team) != str(own_team):
        return True
    tenant = data.get("tenant_id") or data.get("tenantId")
    own_tenant = conn.get("tenant_id")
    if tenant and own_tenant and str(tenant) != str(own_tenant):
        return True
    return False


def home_known(conn: IntakeConnection, event: IntakeEvent) -> bool:
    """FAIL CLOSED: the sender's workspace (Slack team) or tenant (Teams) is KNOWN on both sides
    and equal. Without it a guest check proves nothing, so there is no auto-link."""
    data = event.data or {}
    team, own_team = data.get("user_team_id") or data.get("team_id"), conn.get("team_id")
    if team and own_team:
        return str(team) == str(own_team)
    tenant = data.get("tenant_id") or data.get("tenantId")
    own_tenant = conn.get("tenant_id")
    if tenant and own_tenant:
        return str(tenant) == str(own_tenant)
    return False


# ---------------------------------------------------------------------------------------------
# Finding the person
# ---------------------------------------------------------------------------------------------

def _endpoint(connection: Connection, connection_row: Mapping[str, Any]) -> Optional[dict]:
    endpoint_id = connection_row.get("chat_endpoint_id")
    if not endpoint_id:
        return None
    row = connection.execute(text("SELECT * FROM chat_endpoints WHERE id = :e"),
                             {"e": endpoint_id}).mappings().first()
    return dict(row) if row else None


def _principal_live(connection: Connection, principal_id: str) -> bool:
    from ..identity.users import load_principal

    principal = load_principal(connection, principal_id)
    return principal is not None and principal.active and principal.is_person


def _linked(connection: Connection, endpoint: Mapping[str, Any], external_id: str
            ) -> tuple[Optional[dict], str]:
    """(link row, reason). A link is honoured only while verified, not revoked, and its person
    is still an active user (an Okta deactivation ends it at the next event)."""
    from .chat_channels import find_link

    link = find_link(connection, str(endpoint["id"]), external_id)
    if link is None:
        return None, "unlinked"
    if not link.get("principal_id") or not _principal_live(connection, str(link["principal_id"])):
        return None, "link_principal_inactive"
    return link, "linked"


def _attested(connection_row: Mapping[str, Any]) -> Optional[dict]:
    import json

    cfg = connection_row.get("config")
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg)
        except ValueError:
            cfg = {}
    att = (cfg or {}).get("sso_attested")
    if not isinstance(att, dict) or not att:
        return None
    domains = [str(d).lower().lstrip("@") for d in (att.get("domains") or []) if str(d).strip()]
    if not domains:
        return None
    return {**att, "domains": domains}


def _auto_link(connection: Connection, endpoint: Optional[Mapping[str, Any]],
               connection_row: Mapping[str, Any], conn: IntakeConnection, event: IntakeEvent
               ) -> tuple[Optional[str], Optional[str], str]:
    """SSO-attested email → (principal_id, link_id, method). Never for guests."""
    from ..identity.users import by_email

    attested = _attested(connection_row)
    email = str(event.bossactor.email or "").strip().lower()
    if attested is None or not email or "@" not in email:
        return None, None, "no_attestation" if attested is None else "no_email"
    if is_guest(conn, event):
        return None, None, "guest_never_auto_linked"
    if not home_known(conn, event):
        return None, None, "workspace_unknown"
    domain = email.rsplit("@", 1)[-1]
    if domain not in attested["domains"]:
        return None, None, "domain_not_attested"
    # The attestation holds only while its IdP is still an active provider with that domain.
    idp = connection.execute(text(
        "SELECT domains, status FROM identity_providers WHERE id = :i"),
        {"i": str(attested.get("idp") or "")}).mappings().first()
    if idp is None or idp["status"] != "active" or domain not in {
            str(d).lower().lstrip("@") for d in (idp["domains"] or [])}:
        return None, None, "attestation_lapsed"
    if endpoint is not None and event.bossactor.external_id and connection.execute(text(
            "SELECT 1 FROM chat_identity_links WHERE endpoint_id = :e AND external_user_id = :u "
            "AND revoked_at IS NOT NULL LIMIT 1"),
            {"e": endpoint["id"], "u": event.bossactor.external_id}).scalar():
        # A person (or an admin) revoked this chat user's link: never silently re-linked.
        return None, None, "link_revoked"
    user = by_email(connection, email)
    if user is None or user.status != "active" or not user.email_verified_at:
        return None, None, "no_active_verified_user"
    link_id = None
    if endpoint is not None and event.bossactor.external_id:
        link_id = make_id("chl")
        got = connection.execute(text("""
            INSERT INTO chat_identity_links (id, endpoint_id, external_user_id, external_name,
                   principal_id, person_actor, verified_at, method, external_email, attested_by)
            VALUES (:i, :e, :u, :n, :p, :a, now(), 'sso_email', :em, :by)
            ON CONFLICT DO NOTHING RETURNING id"""),
            {"i": link_id, "e": endpoint["id"], "u": event.bossactor.external_id,
             "n": (event.bossactor.display or "")[:200], "p": user.principal_id,
             "a": _person_actor(user), "em": email, "by": str(attested.get("attested_by") or "")}
        ).scalar()
        if got is None:
            # An unverified row (a pending code) already exists for this user: it becomes the link.
            got = connection.execute(text("""
                UPDATE chat_identity_links SET principal_id = :p, person_actor = :a,
                       verified_at = COALESCE(verified_at, now()), method = 'sso_email',
                       external_email = :em, attested_by = :by, link_code_hash = NULL,
                       code_expires_at = NULL, updated_at = now()
                 WHERE endpoint_id = :e AND external_user_id = :u AND revoked_at IS NULL
                 RETURNING id"""),
                {"p": user.principal_id, "a": _person_actor(user), "em": email,
                 "by": str(attested.get("attested_by") or ""), "e": endpoint["id"],
                 "u": event.bossactor.external_id}).scalar()
        link_id = str(got) if got else None
        emit_event(connection, str(connection_row["project_id"]), "chat.link_confirmed",
                   "intake", "chat_endpoint", str(endpoint["id"]),
                   {"kind": endpoint.get("kind"), "principal_id": user.principal_id,
                    "external_user_id": event.bossactor.external_id, "method": "sso_email",
                    "idp": attested.get("idp")})
    return user.principal_id, link_id, "sso_email"


def _person_actor(user: Any) -> str:
    ids = getattr(user, "legacy_member_ids", None) or ()
    return str(ids[0]) if ids else str(user.principal_id)


def _github_person(connection: Connection, event: IntakeEvent, org_id: Optional[str]
                   ) -> Optional[str]:
    """The sender's own GitHub sign-in (issuer github, subject = numeric id) → principal, in
    the connection's org only."""
    if event.bossactor.kind != "user" or not event.bossactor.external_id:
        return None
    sender_id = str((event.data or {}).get("sender_id") or
                    ((event.data.get("sender") or {}) if isinstance(event.data.get("sender"), dict)
                     else {}).get("id") or "")
    login = event.bossactor.external_id
    row = connection.execute(text("""
        SELECT i.principal_id FROM identity_user_identities i
          JOIN identity_principals p ON p.id = i.principal_id
         WHERE lower(i.issuer) IN ('github', 'https://github.com')
           AND (i.subject = :sid OR lower(i.subject) = lower(:login))
           AND p.status = 'active' AND p.org_id = :org LIMIT 1"""),
        {"sid": sender_id or "-", "login": login, "org": org_id or "-"}).scalar()
    return str(row) if row else None


def _mail_person(connection: Connection, connection_row: Mapping[str, Any],
                 conn: IntakeConnection, event: IntakeEvent) -> tuple[Optional[str], str]:
    """Mail sender identity (EVENTING_LLD.md §6.2, P9c-T2): a `From` address resolves to a BOSS
    person ONLY when `Authentication-Results` proves `dmarc=pass` aligned with the `From` domain
    AND that domain is an active verified domain of the connection's org (or SSO-attested domain),
    AND an active verified user holds that email. A DMARC-failing or missing-DMARC `From` NEVER
    resolves to a person."""
    from ..connectors.intake._mail import email_domain, parse_dmarc
    from ..identity.users import by_email

    email = str(event.bossactor.email or (event.data or {}).get("from_email") or "").strip().lower()
    if not email or "@" not in email:
        return None, "no_email"
    data = event.data or {}
    dmarc = data.get("dmarc") if isinstance(data.get("dmarc"), Mapping) else None
    if dmarc is None:
        raw_ar = data.get("authentication_results")
        vals = raw_ar if isinstance(raw_ar, list) else ([raw_ar] if raw_ar else [])
        dmarc = parse_dmarc(vals, email)
    if dmarc.get("passed") is not True:
        return None, "dmarc_failed"
    domain = email_domain(email)
    if not domain:
        return None, "no_email"
    org = connection.execute(text(
        "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
        "WHERE p.id = :p"), {"p": conn.project_id}).scalar()
    verified_domains: set[str] = set()
    if org:
        rows = connection.execute(text(
            "SELECT domains FROM identity_providers WHERE org_id = :o AND status = 'active'"),
            {"o": org}).all()
        for row in rows:
            for d in (row[0] or []):
                cleaned = str(d).strip().lower().lstrip("@")
                if cleaned:
                    verified_domains.add(cleaned)
    attested = _attested(connection_row)
    if attested:
        verified_domains.update(attested["domains"])
    if domain not in verified_domains:
        return None, "domain_not_verified"
    user = by_email(connection, email)
    if user is None or user.status != "active" or not user.email_verified_at:
        return None, "no_active_verified_user"
    return user.principal_id, "dmarc_email"


def find_person(connection: Connection, connection_row: Mapping[str, Any], conn: IntakeConnection,
                event: IntakeEvent) -> tuple[Optional[str], Optional[str], str, str]:
    """(principal_id, link_id, method, reason) for the event's actor, per source."""
    actor = event.bossactor
    if actor.kind != "user" or not (actor.external_id or actor.email):
        return None, None, "", f"actor is {actor.kind or 'unknown'}, not a person"
    if conn.kind in MAIL_KINDS:
        principal, reason = _mail_person(connection, connection_row, conn, event)
        if principal:
            return principal, None, "dmarc_email", "dmarc pass + verified org domain"
        return None, None, "", reason
    endpoint = _endpoint(connection, connection_row)
    if endpoint is not None and actor.external_id:
        link, reason = _linked(connection, endpoint, actor.external_id)
        if link is not None:
            return str(link["principal_id"]), str(link["id"]), str(link.get("method") or "code"), \
                "linked"
        if reason == "link_principal_inactive":
            return None, None, "", reason
    if conn.kind == "github":
        org = connection.execute(text(
            "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
            "WHERE p.id = :p"), {"p": conn.project_id}).scalar()
        principal = _github_person(connection, event, org)
        if principal:
            return principal, None, "github", "github sign-in"
    principal, link_id, method = _auto_link(connection, endpoint, connection_row, conn, event)
    if principal:
        return principal, link_id, method, "sso attested"
    return None, None, "", method


# ---------------------------------------------------------------------------------------------
# The person's authority and the prompts
# ---------------------------------------------------------------------------------------------

def _agent_ids(connection: Connection, project_id: str, agent_key: str) -> list[str]:
    from ..identity import consent

    try:
        return [a.agent_id for a in consent._agents(connection, project_id, [agent_key])]  # noqa: SLF001
    except Exception:  # noqa: BLE001 — identity not set up here: no passports to cover
        return []


def person_acting(connection: Connection, principal_id: str, *, project_id: str, agent_key: str
                  ) -> tuple[Any, str]:
    """(Acting, reason). The person's standing grant that covers this agent in this project;
    under enforce a person with none gets no Acting (`no_grant`)."""
    from ..identity import grants, lineage
    from ..identity.users import load_principal

    principal = load_principal(connection, principal_id)
    if principal is None or not principal.active:
        return None, "principal_inactive"
    agent_ids = _agent_ids(connection, project_id, agent_key)
    grant = None
    if agent_ids:
        try:
            grant = grants.find_covering(principal_id, agent_ids=agent_ids, permissions=frozenset(),
                                         project_id=project_id, repos=(), run_id=None,
                                         db=connection)
        except Exception:  # noqa: BLE001 — a grant lookup that fails is "no grant", never a run
            logger.debug("grant lookup failed", exc_info=True)
            grant = None
    if grant is not None:
        return lineage.Acting(principal_id=principal_id, org_id=grant.org_id, grant_id=grant.id), \
            "standing grant"
    if lineage._enforced():  # noqa: SLF001 — the one mode source every door reads
        return None, "no_grant"
    return lineage.Acting(principal_id=principal_id, org_id=principal.org_id), "no grant (authz off)"


def _bucket(now: datetime) -> int:
    return int(now.timestamp() // PROMPT_BUCKET_SECONDS)


def _dm(connection: Connection, endpoint: Mapping[str, Any], user: str, words: str,
        dedupe: str) -> bool:
    from .notify import enqueue_chat

    got = enqueue_chat(connection, endpoint_id=str(endpoint["id"]),
                       project_id=str(endpoint["project_id"]), kind="message",
                       payload={"op": "dm", "user": user, "text": words}, dedupe_key=dedupe)
    return got is not None


def prompt_link(connection: Connection, settings: Any, connection_row: Mapping[str, Any],
                event: IntakeEvent, *, now: Optional[datetime] = None, force: bool = False) -> bool:
    """The unknown sender's private link prompt (chat_channels' one-time code), at most once
    per user per hour: the outbox dedupe key carries the hour, so a second event in the same
    hour writes nothing. `force` (a person pressed Resend) mints a new code and a new dedupe
    key, so it sends inside that hour. False when there is no chat endpoint to answer through."""
    from .chat_channels import _link_words, start_link

    endpoint = _endpoint(connection, connection_row)
    user = event.bossactor.external_id
    if endpoint is None or not user:
        return False
    now = now or _now()
    if force:
        dedupe = f"link_dm:intake:{endpoint['id']}:{user}:resend:{make_id('r')}"
    else:
        dedupe = f"link_dm:intake:{endpoint['id']}:{user}:{_bucket(now)}"
        exists = connection.execute(text("SELECT 1 FROM chat_outbox WHERE dedupe_key = :d"),
                                    {"d": dedupe}).scalar()
        if exists:
            return False
    code = start_link(connection, endpoint, user, event.bossactor.display or "", now)
    return _dm(connection, endpoint, user, _link_words(settings, code), dedupe)


def prompt_grant(connection: Connection, settings: Any, connection_row: Mapping[str, Any],
                 event: IntakeEvent, *, agent_key: str, now: Optional[datetime] = None) -> bool:
    """A linked person without a grant for this agent: one private message an hour with the
    console path where they grant it."""
    from .chat_channels import _base_url

    endpoint = _endpoint(connection, connection_row)
    user = event.bossactor.external_id
    if endpoint is None or not user:
        return False
    now = now or _now()
    dedupe = f"grant_dm:intake:{endpoint['id']}:{user}:{agent_key}:{_bucket(now)}"
    base = _base_url(settings)
    where = f"{base}{GRANT_PATH}" if base else f"the console under Account › Agents ({GRANT_PATH})"
    words = (f"The agent {agent_key} has no permission to act for you yet. Open {where} and "
             f"grant it; your message waits up to {int(GRANT_WAIT.total_seconds() // 60)} minutes "
             "and is answered once the grant lands.")
    return _dm(connection, endpoint, user, words, dedupe)


# ---------------------------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------------------------

def resolve(connection: Connection, settings: Any, *, connection_row: Mapping[str, Any],
            conn: IntakeConnection, event: IntakeEvent, route: Mapping[str, Any],
            agent_key: str, now: Optional[datetime] = None) -> Subject:
    """The subject of the run route `route` would start for `event` (§6.1, §6.4)."""
    now = now or _now()
    mode = str(route.get("identity_mode") or default_mode(conn.kind))
    if mode not in MODES:
        mode = default_mode(conn.kind)
    project_id = str(route["project_id"])
    if mode == "owner":
        return Subject(mode=mode, kind="owner",
                       external_id=event.bossactor.external_id, display=event.bossactor.display,
                       reason="the route owner's standing grant")
    principal, link_id, method, reason = find_person(connection, connection_row, conn, event)
    if principal is None:
        if mode == "person_or_owner":
            return Subject(mode=mode, kind="owner", external_id=event.bossactor.external_id,
                           display=event.bossactor.display,
                           reason=f"no linked person ({reason}); the owner's grant, the actor "
                                  "named as data")
        prompted = reason in ("unlinked", "no_attestation", "no_email", "domain_not_attested",
                              "no_active_verified_user", "guest_never_auto_linked",
                              "workspace_unknown", "attestation_lapsed", "link_revoked") and \
            prompt_link(connection, settings, connection_row, event, now=now)
        return Subject(mode=mode, kind="none", external_id=event.bossactor.external_id,
                       display=event.bossactor.display, reason=f"unlinked ({reason})",
                       prompted=prompted)
    acting, why = person_acting(connection, principal, project_id=project_id, agent_key=agent_key)
    subject = Subject(mode=mode, kind="person", principal_id=principal, acting=acting,
                      link_id=link_id, method=method or None,
                      external_id=event.bossactor.external_id, display=event.bossactor.display,
                      reason=f"{reason}; {why}")
    if acting is None:
        if why == "principal_inactive":
            subject.kind = "none"
            subject.reason = "the linked person is no longer active"
            return subject
        subject.deferred_until = now + GRANT_WAIT
        subject.prompted = prompt_grant(connection, settings, connection_row, event,
                                        agent_key=agent_key, now=now)
    return subject


__all__ = ["CHAT_KINDS", "GRANT_WAIT", "LINK_METHODS", "MODES", "Subject", "allowed_modes",
           "default_mode", "find_person", "is_guest", "person_acting", "prompt_grant",
           "prompt_link", "resolve"]
