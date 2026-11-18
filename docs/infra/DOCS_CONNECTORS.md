# Document-system connectors (BOSSAPP-1384): Confluence

BOSS connects to the team's Confluence once for the org. Agents search and read pages, propose pages
a person approves, and chosen spaces are kept in BOSS's knowledge.

## How the operator connects it

1. Create an Atlassian API token: id.atlassian.com › Security › API tokens (the account must be able
   to see the spaces the agents should read, and to edit the ones they should publish to).
2. In the console: **Setup › Ticket systems › Confluence › Connect Confluence**.
3. Site URL: `https://<site>.atlassian.net/wiki`. Email: the account that owns the token. Paste the
   token. Or tick **Use the org's Jira credential** when Jira is connected for the same Atlassian
   site: the email and token are copied into Confluence's own vault entry (disconnecting Jira later
   does not break Confluence).
4. **Test and connect.** BOSS calls the site once with the credential; nothing is stored unless it
   works. The token goes to the secret backend; the row keeps only a reference, the last four
   characters and a fingerprint.
5. Optional: *Default spaces* (what `confluence_search` filters by when an agent names none) and
   **Choose spaces** (the spaces kept in knowledge), then **Sync now**.

**Test connection** (row menu) makes one cheap call and says who BOSS signed in as. **Disconnect**
removes the token reference; the chosen spaces are kept for a reconnect, and knowledge already
synced stays.

## What is where

| | |
|---|---|
| Client, CQL, storage-format ↔ markdown | `engine/boss_engine/connectors/docs/` |
| Connection (org-wide, by reference) | `services/docs_connections.py`, table `docs_connections` |
| Publish = proposal → approval → versioned write | `services/confluence_publish.py` (`change_consents`, decision action `apply_confluence_publish`) |
| Knowledge source (spaces → `spec` nodes with provenance) | `services/confluence_knowledge.py` |
| Agent tools | `tools/confluence_tools.py`: `confluence_search`, `confluence_read_page`, `confluence_publish_page` |
| Skills / roles | `confluence.read` (roles that read tickets, and `docs_writer`), `confluence.publish` (`docs_writer` only) |
| API | `apps/api/app/routers/docs_connections.py` (`/v1/docs-connections*`) |
| Console | `apps/web/app/setup/confluence-row.tsx`, `apps/web/lib/docs-connections.ts` |

## Behaviour that matters

* **Reads**: CQL search (`text ~`, `space in`, `label in`, raw `cql`, paged by `start`, at most 50
  per call); a page by id, by URL (`/pages/<id>/`, `viewpage.action?pageId=`) or title + space, as
  markdown with links kept, its version, labels and last-modified; attachments metadata; a space's
  page tree. A body is cut at `max_chars` (default 20 000, hard cap 60 000) and says `truncated`.
* **Publish** never writes, and needs `docs:publish` (only the `documentation` preset, `quill`, holds
  it; reads need `docs:read`, held by the roles that read tickets and by `quill`). `confluence_publish_page` parks the exact change as a diff-bound consent
  and an "Approve a Confluence page" decision. Approving records the intent and COMMITS; the
  write follows after the commit (never inside the decision's transaction) as a *versioned* update:
  the consent records the version the author read, and a page that moved since, or a 409 from
  Confluence, is not overwritten. The failure is recorded on the consent (rejected) and as
  `docs.confluence_publish_failed`. A write that was claimed but never finished is re-claimed after
  ten minutes, and each sync start catches up unfinished writes. Creating a page takes a space and an
  optional parent.
* **Knowledge**: each page becomes a `spec` node `confluence:<site>:<page id>` with `space`,
  `page_id`, `version`, `url`, `last_modified`, `labels`; its scrubbed text is captured. Sync is ONE
  background job (`POST /v1/docs-connections/sync` answers 202; `GET` polls status and per-space
  progress). Every database step is its own short transaction. A stalled or failed job resumes
  (finished spaces are skipped); only pages modified since the last sync are asked for, and a page
  whose version is already held is skipped. Synced text is returned from knowledge search marked
  untrusted, like the live read.
* **Egress**: every request goes through the ticket-connector HTTP helper, which sends only to the
  connection's own site host (https), after a host check; the site must be `*.atlassian.net` unless
  the operator allows another host (`BOSS_CREDENTIAL_HOSTS=confluence=<host>`). A `confluence`
  provider binding (`*.atlassian.net`, `api.atlassian.com`) keeps a host credential from going
  anywhere else.
* **Redirects and plain http**: the shared HTTP path never follows a redirect to another host (or
  from https), so a credential is never forwarded; a credentialed call over plain http is refused
  only for PUBLIC hosts (loopback, private addresses, single-label compose names and `.local` /
  `.internal` / `.lan` hosts keep working for self-hosted Jira or Plane). This covers Jira too.
* **Writes are never re-sent**: the Confluence client does not retry a POST/PUT after a timeout or
  a 5xx. After any failed write the page is read back; if it holds what was written it is a
  success. Sync state and the sync job are kept per project (knowledge is per project).
* **Restarts**: unfinished approved writes are finished by the worker's monitoring tick, claim-fenced
  so one writer records the outcome. A real failure raises a notice card for the approver.
* **Upgrades**: the identity upgrade moves an existing `quill` from the `writer` preset to
  `documentation` (additive; a custom ceiling is never touched).
* **Jira credential reuse** needs `integration:manage` where Jira lives and always uses Jira's own
  site; a reconnect without a token keeps the stored token only for the same site and email.
  Disconnect revokes and deletes the vault entry this connection stored.
* **Secrets**: the token is resolved only when a request is sent; it is never in settings, events,
  logs, errors or API answers.
* **Page text is outside text**: results pass the tool-result screen and the low-trust ledger, so a
  later write that carries flagged page text asks a person first.

## Seams, not built

* **Data Center / Server**: `DEPLOYMENTS["datacenter"]` names its API root (`/rest/api`) and PAT
  (bearer) auth; connecting one is refused with a plain message until an adapter exists.
* **OAuth 2.0 (3LO)**: the client already takes `auth_method: bearer`; a 3LO connection would address
  `https://api.atlassian.com/ex/confluence/<cloudId>` as its base. The sign-in flow is not built.
