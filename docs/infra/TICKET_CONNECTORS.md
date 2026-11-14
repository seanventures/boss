# Ticket connectors

BOSS reads, browses and updates tickets in the systems teams already use (Jira, Plane, GitHub
Issues, ...) through ONE contract. Plane epic: BOSSAPP-1047. Phase A (this document) is the
framework and the connectors. Phase B adds the API routes, the console and the agent tools on
top of the same contract.

Code: `engine/boss_engine/connectors/tickets/`. Tests: `engine/tests/ticket_connectors/`.

## The contract

`contract.py` holds the normalised shapes. Every connector returns these and nothing
provider-specific.

| Shape | Fields |
|---|---|
| `Ticket` | provider, key, id, url, title, body_markdown, type (`bug`/`story`/`task`/`epic`/`other`), raw_type, status, status_category (`todo`/`doing`/`review`/`done`), priority, labels, assignee, reporter, acceptance_criteria, comments (the latest N: author, at, body), parent_key, children_count, attachments (name, url, size only), updated_at, screen |
| `TicketRef` | provider, key, url, id, project |
| `TicketProject` | key, name, id, url, open_count, epic_count |
| `TicketEvent` | provider, kind (`created`/`updated`/`deleted`/`commented`/`other`), ref, actor, changed, raw_event |
| `TransitionResult`, `LinkResult` | what a transition or a link did, and whether anything changed |

Each adapter extends `TicketAdapterBase` (`base.py`) and offers this public API:

| Method | Capability |
|---|---|
| `test_connection(configuration)` | — |
| `parse_ref(text, conn=)` | read |
| `get_ticket(conn, ref)` | read |
| `search(conn, query, project=None, filters={}, limit=50)` | browse |
| `list_projects(conn, query=None, counts=False)` | browse |
| `list_epics(conn, project)` | browse |
| `list_children(conn, epic_ref)` | browse |
| `list_unparented(conn, project)` | browse |
| `comment(conn, ref, markdown)` | write_comment |
| `transition(conn, ref, to_category)` | transition |
| `link(conn, ref, url, title)` | link |
| `create(conn, title=, body_markdown=, type=, labels=, project=, parent=)` | create |
| `verify_webhook(headers, body, secret)` | webhook |
| `parse_webhook(body)` | webhook |

The base class implements the public methods once. Each one:

1. checks the capability flag;
2. calls the connector's hook (`fetch_ticket`, `search_tickets`, `fetch_projects`,
   `fetch_epics`, `fetch_children`, `fetch_unparented`, `post_comment`, `apply_transition`,
   `add_link`, `create_ticket`, `check_webhook`, `read_webhook`, `ref_from_url`);
3. screens the text before it returns.

If the manifest declares a capability false, the method raises `CapabilityNotSupported`.

Errors are typed, and all of them are `WorkTrackerHttpError` subclasses: `AuthFailed` (with a
`reason` of `rejected`, `no_credential` or `needs_person_sign_in`), `NotFound`, `RateLimited`
(with `retry_after`), `HostNotAllowed` and `CapabilityNotSupported`. No message ever contains a
credential.

### Status categories

A connector maps the provider's workflow onto four categories:

- **Jira** uses `statusCategory.key`: `new` is todo, `indeterminate` is doing, `done` is done.
- **Plane** uses the state group: backlog, unstarted and triage are todo; started is doing;
  completed and cancelled are done.

Neither system has a review category. A connection's `settings.status_mapping` (category →
status name) wins in both directions. Reading maps a mapped status name back to its category.
`transition` moves the ticket to the mapped status, or to the first status in the target
category. The manifests default `review` to the status named "In Review".

### Screening

`get_ticket`, and every ticket that a browse call returns, passes its title, body, acceptance
criteria and comments through `tool_result_screen.screen_result`. The verdict is stored on
`Ticket.screen` as `{mode, verdict: clean|suspicious|unscreened, rules}`, and
`Ticket.untrusted` is true when the verdict is suspicious. The text itself is never rewritten.
Phase B decides how to present the verdict.

### Reference parsing

`parse_ref` recognises the connector's own URLs:

- Jira `/browse/KEY-1` and `?selectedIssue=KEY-1`;
- Plane `/projects/<uuid>/issues/<uuid>` and `/browse/KEY-1`;
- GitHub `/<owner>/<repo>/issues/N`.

It also recognises bare keys (`[A-Z][A-Z0-9_]+-\d+`), but only when the prefix is a known
project key of that connection. The known keys are the configured `project_key` and
`settings.project_keys`. Bare keys are never matched without known keys, so `UTF-8`, `SHA-256`,
`ISO-8601` and `CVE-2024-1234` never match. `service.parse_refs(text, connections)` runs this
across every connected system.

## The manifest

Each connector lives in its own folder, `connectors/tickets/<key>/`, which holds a
`manifest.yaml` and an `adapter.py`. The registry (`registry.py`) loads every folder. For each
one it:

- validates the manifest against `MANIFEST_SCHEMA`;
- imports the adapter class;
- checks that the adapter implements every hook for each capability the manifest claims, and
  no capability the manifest declares false;
- registers the connector's hosts with the outbound credential guard
  (`env_refs.register_provider_hosts`).

`work_tracker_adapters.TRACKER_ADAPTERS`, `get_adapter` and `list_catalog` read from this
registry, so existing callers are unchanged. The five original adapter classes still exist, and
each registered adapter is a subclass of one of them.

| Field | Meaning |
|---|---|
| `key`, `label`, `icon`, `description`, `docs_url`, `order` | Identity and catalog order. `key` must equal the folder name. |
| `adapter` | The class name in `adapter.py` |
| `auth_fields` | Connection fields: `{name, label, secret, placeholder, required, help}` |
| `auth_kinds` | How a connection signs in (see below). The default is one `api_token` kind. |
| `capabilities` | `read`, `browse`, `write_comment`, `transition`, `link`, `create`, `webhook`: each true or false |
| `default_hosts` | Official hosts, https only (`*.x` matches subdomains) |
| `default_base_url`, `base_url_field`, `base_url_fields` | Where requests go; the configured base URL's host is allowed |
| `guard_provider`, `operator_base_url_env` | The env_refs provider binding, and the host settings an operator sets for a self-hosted instance |
| `key_refs` | Whether bare keys are references |
| `status_mapping_defaults` | category → status name |
| `webhook` | `{scheme: hmac-sha256 / shared-token / hmac-sha256-or-shared-token, header, ...}` |

## Credentials

Agents never see tokens. A `TicketConnection` carries the stored configuration: the base URL,
the workspace, and a `secret_ref_id` or `credential_ref`, never a value that is shown or
printed. `repr` hides every token key. The credential is resolved server-side, when the shared
HTTP helper attaches it to a request.

### Auth kinds

A connection may hold more than one auth kind:

```yaml
auth_kinds:
  - kind: api_token            # the connection's bot / API key
    fields: [email, api_token]
  - kind: oauth2               # one sign-in per person (Jira: Atlassian OAuth 2.0 3LO)
    authorize_url: https://auth.atlassian.com/authorize
    token_url: https://auth.atlassian.com/oauth/token
    audience: api.atlassian.com
    scopes: [read:jira-work, write:jira-work, read:jira-user, offline_access]
    pkce: false
    per_user: true
    resource_discovery_url: https://api.atlassian.com/oauth/token/accessible-resources
    api_base_template: https://api.atlassian.com/ex/jira/{resource_id}
```

`auth.select_credential(conn, adapter)` decides the credential for each call. It uses the acting
person's own OAuth sign-in when they have one; an agent acting for a person needs that person's
delegation grant to lend it (`kit: {as_user: true}`). Otherwise it uses the connection's bot
token. If neither is available, it raises `AuthFailed(reason="needs_person_sign_in")`.

`configuration.credential_policy` narrows this choice:

- `shared`: the bot token only;
- `per_user`: the person's own sign-in only;
- `per_user_fallback`: the person's own sign-in, else the bot token. This is the default when
  the connector has a per-user OAuth kind.

Every call records the identity it acted as in two places: in `conn.attribution`, and as a
`ticket_system.call` event when the project database is available.

### Storage and refresh

There is no new table:

- A person's tokens are a `connection_credentials` row (`connection_id = kit:tickets:<provider>`,
  subject `user`). The secret itself is an identity-vault item.
- `begin_oauth` keeps the pending sign-in in `mcp_oauth_pending`: the state hash, the PKCE
  verifier (when the provider supports PKCE), the redirect URI and who started it. The pending
  row is single-use and expires after ten minutes.
- The OAuth `state` is HMAC-signed with a key derived from the vault, and it is bound to the
  project, the connection and the person. `complete_oauth` refuses a state that was forged,
  already used, expired, or started by somebody else.
- Atlassian refresh tokens rotate. The refresh is `vault.connection_token`'s single-flight
  refresh:
  - it locks the row (`FOR UPDATE`) in the vault's own transaction;
  - it re-reads the row and refreshes only once;
  - it commits the new refresh token before the call continues.

  So two calls never race, and a rollback by the caller cannot lose a token that the provider
  has already burned. A refused refresh marks the credential `needs_reauth`, and the call then
  raises `needs_person_sign_in`, or falls back to the bot token when the policy allows it.
- After the code exchange, `complete_oauth` calls `accessible-resources` and picks the site whose
  URL matches the connection's `base_url`. It keeps that site's cloudId in
  `configuration.oauth_resource_id` (not a secret). Later API calls go to
  `https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...`.

Phase B wires three functions to routes: `begin_oauth(conn, person, redirect_uri=)` returns
`{authorize_url, state}`; `complete_oauth(conn, state, code, completed_by=)`; and
`disconnect_person(conn, person)`.

The OAuth app (client id and secret) is resolved in this order:

1. `configuration.oauth_client_id` with `oauth_client_secret_ref` (a stored secret reference);
2. the operator's `BOSS_<PROVIDER>_OAUTH_CLIENT_ID` and `_SECRET`.

## The host allowlist

Every request goes through `http.TicketHttp`, the one shared helper. It allows a URL only when
the host is one of these:

- a manifest `default_hosts` entry, over https;
- the host of the base URL configured on this connection;
- an operator base URL for the provider (`PLANE_BASE_URL`, `JIRA_BASE_URL`, ...) or a
  `BOSS_CREDENTIAL_HOSTS` entry.

Cloud metadata addresses and link-local addresses are always refused. Any other host raises
`HostNotAllowed`, which is checked before anything is sent. This covers a pagination link that
points somewhere else.

A self-hosted Plane, such as `https://plane.local.sean.network`, works because it is the
connection's `api_base_url`. A host credential from the environment (`env:PLANE_API_KEY`) stays
bound to its provider's hosts through `env_refs.assert_may_send`, and the process-wide
`check_outbound` still runs under every request.

The helper also sets timeouts and retries 429 and 5xx responses with backoff. It honours
`Retry-After`, capped at 30 seconds. It paginates with each provider's own cursor style:

- Jira `nextPageToken`;
- Jira `startAt`;
- Plane `next_cursor` / `next_page_results`.

## Per-connection settings

Settings live in `work_tracker_providers.configuration["settings"]`. They are non-secret JSON
stored beside the connection they configure, and `connection_settings()` reads them with
defaults and type checks:

| Setting | Default |
|---|---|
| `status_mapping` | the manifest defaults |
| `pickup_label` | `"boss"` |
| `auto_start` | `false` ("ask me first"). Only a real `true` turns it on. |
| `poll_interval_s` | 300, with a floor of 60 |
| `acceptance_criteria_field` | e.g. a Jira custom field |
| `epic_label` | `"epic"` (Plane) |
| `project_keys` | — |
| `comments_limit` | 10 |

These settings did not need a schema change: the configuration is already per connection, it is
already JSONB, and it is already written by the connect path. One caveat for Phase B:
`connect_tracker_provider` replaces the whole configuration, so a settings editor must merge
with the stored configuration rather than send a partial one.

## Connector notes

- **Jira Cloud (REST v3):**
  - search uses JQL through `/rest/api/3/search/jql` (the old `/search` is being retired);
  - projects come from `/rest/api/3/project/search`;
  - epics are `issuetype = Epic`; children are `parent = KEY`;
  - counts come from `/search/approximate-count`;
  - transitions use `/transitions`; PR links are remote links with a stable `globalId`;
  - bodies and comments are converted from ADF to markdown, and BOSS's comments from markdown
    to ADF.

  For webhooks, Jira admin webhooks registered with a secret send
  `X-Hub-Signature: sha256=<HMAC-SHA256 of the raw body>`. A webhook registered without a
  secret carries `?token=<secret>` on its URL, and the route passes that token as
  `X-Boss-Webhook-Token`.
- **Plane:**
  - it uses the `/api/v1/workspaces/<slug>/...` API at the connection's base URL, with
    `X-API-Key`;
  - an issue is found by uuid or by `IDENT-N`;
  - modules and cycles are not epics: an epic is a parent issue, an issue with the epic label,
    or an issue whose type is named Epic;
  - children are found through `parent`;
  - webhooks use the existing `X-Plane-Signature` HMAC (`plane_webhooks.verify_signature`).
- **GitHub Issues:** read (`get_ticket`, `parse_ref` for URLs and `owner/repo#N` of the
  connection's own repository), comment, and create. Browse, transition, link and webhooks are
  declared false.
- **Linear and Azure DevOps:** create and test, unchanged. Everything else is declared false.

## Conformance kit: adding a connector

`engine/tests/ticket_connectors/conformance.py` is the shared suite. It runs against:

- Jira and Plane, each with a fake HTTP server on 127.0.0.1 that serves the real response
  shapes: pagination, ADF, errors, 401, and 429 with `Retry-After`;
- `fake_connector`, a scripted in-memory adapter that is loaded from its own folder the same way
  a plug-in is.

The suite checks the normalised fields, the latest N comments, URL and key references, look-alike
keys, NotFound, AuthFailed (and that the token is never echoed), retries, pagination, projects,
epics, children and unparented tickets, comments, transitions (by category and by mapping),
idempotent links, create, webhooks, the host guard, and screening. A capability declared false
must raise `CapabilityNotSupported`.

To add ServiceNow (for example):

1. Create `connectors/tickets/servicenow/manifest.yaml` and `adapter.py`. The adapter extends
   `TicketAdapterBase` and implements the hooks for the capabilities it claims.
2. Write `tests/ticket_connectors/fake_servicenow.py`, a fake server with ServiceNow's response
   shapes. It serves the dataset in `dataset.py`.
3. Add a `Harness` subclass to `HARNESSES` in `conformance.py`.
4. Run `pytest tests/ticket_connectors` and make it pass. The registry test fails if the manifest
   and the adapter disagree.

The OAuth flows are tested in `test_ticket_oauth.py` against the test database and the fake
Jira's `/oauth/token` and `accessible-resources`.

### Live Jira run (optional)

```bash
SSL_CERT_FILE=$(python3 -m certifi) BOSS_TICKET_LIVE=jira \
  python3 -m pytest engine/tests/ticket_connectors/test_jira_live.py
```

The run reads the macOS Keychain item `boss-jira-test`: the password is the API token, and the
account is the login email. The token is never printed. The run does read-only checks first.
Then it creates one ticket labelled `boss-conformance` in KAN, comments on it, moves it from
doing to done, links it, and leaves it in Done. It skips when the Keychain item is missing.
CI runs only the fakes.
