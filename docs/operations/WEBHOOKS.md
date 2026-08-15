# Webhooks

BOSS supports durable outbound webhooks and signed inbound GitHub webhooks. Outbound calls run as leased jobs, create a Receipt per attempt, and become visible retries or deadletters instead of failing silently. Inbound calls are verified against the exact raw request bytes before JSON parsing or routing.

## Configure secrets

Inject secret values into the API runtime; do not place raw secrets in endpoint rows, job payloads, or source control.

```dotenv
BOSS_OUTBOUND_EXAMPLE_SECRET=replace-me
BOSS_WEBHOOK_INBOUND_SECRET=replace-me
BOSS_WEBHOOK_INBOUND_SECRETS_JSON={"github":"env:BOSS_GITHUB_WEBHOOK_SECRET"}
BOSS_WEBHOOK_PROJECT_MAP_JSON={"seanheiney/boss":"boss-dogfood"}
BOSS_WEBHOOK_TIMEOUT_SECONDS=10
```

`BOSS_WEBHOOK_INBOUND_SECRETS_JSON` overrides the single inbound secret for a named source. Values may be supplied directly by the environment variable or as `env:NAME` references. `BOSS_WEBHOOK_PROJECT_MAP_JSON` maps GitHub `owner/repository` names to BOSS project IDs. The configured `BOSS_GITHUB_OWNER/BOSS_GITHUB_REPO` also routes to the default project.

## Register an outbound endpoint

```bash
curl -X POST "$BOSS_URL/v1/webhook-endpoints" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "project_id":"boss-dogfood",
    "url":"https://receiver.example/webhooks/boss",
    "secret_ref":"env:BOSS_OUTBOUND_EXAMPLE_SECRET",
    "data_boundary":"internal",
    "actor":"ceo"
  }'
```

Use the returned endpoint ID as the `sink_ref` of a `webhook_out` event subscription, or queue a manual delivery through `POST /v1/webhook-endpoints/{endpoint_id}/deliver`. Reusing the same project-scoped `dedupe_key` returns the existing delivery and job without another external effect.

Register event subscriptions through the same project-scoped registry. These
examples cover the three initial reactive consumers; keep a subscription
disabled until its worker capability is deployed.

```bash
# Documentation consumer
curl -X POST "$BOSS_URL/v1/event-subscriptions" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"boss-dogfood","event_type":"code_task.pr_opened","sink_kind":"job","sink_ref":"docs.update","enabled":true,"actor":"ceo"}'

# Conversational orchestrator
curl -X POST "$BOSS_URL/v1/event-subscriptions" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"boss-dogfood","event_type":"plan.dispatched","sink_kind":"job","sink_ref":"agent.orchestrator.next_move","enabled":true,"actor":"ceo"}'

# GitHub community triage
curl -X POST "$BOSS_URL/v1/event-subscriptions" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"boss-dogfood","event_type":"webhook.inbound.received","sink_kind":"job","sink_ref":"github.triage","enabled":true,"actor":"ceo"}'
```

List the registry with `GET /v1/event-subscriptions?project_id=...` and use
`PATCH /v1/event-subscriptions/{subscription_id}` to enable or disable a route.
An exact event type or a suffix glob such as `code_task.*` is accepted, but the
engine also applies its fixed dispatchable-event allowlist.

Endpoint mutations and manual queueing require the project actor's `comms_manage` permission. The execute endpoint is worker-only: it validates the worker ID, active lease token, lease expiry, job kind, project, and delivery ownership before making any external call.

The receiver gets canonical JSON and these headers:

- `X-Boss-Event`: canonical event type
- `X-Boss-Delivery`: durable BOSS delivery ID
- `X-Hub-Signature-256`: `sha256=` plus the HMAC-SHA256 of the exact request bytes

Only endpoints whose `data_boundary` is at least as restrictive as the event may receive it.

## Enable the GitHub App community surface

Use [`.github/boss-app-manifest.json`](https://github.com/seanheiney/boss/blob/main/.github/boss-app-manifest.json) as the App-manifest template. Before submitting it through GitHub's App setup flow, replace `boss.example` with the public HTTPS host of the BOSS API. Install the resulting App on only the repositories BOSS should manage, then record its App id, installation id, and generated private key:

```dotenv
BOSS_GITHUB_APP_ID=123456
BOSS_GITHUB_APP_INSTALLATION_ID=789012
BOSS_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/boss-github-app.pem
```

The manifest requests `contents: write`, `issues: write`, and `pull_requests: write`. It subscribes to `issues`, `issue_comment`, `pull_request`, and `pull_request_review`. BOSS narrows each installation token to one configured repository and those permissions. Community replies and reviews use the same short-lived installation-token path as existing PR creation; there is no second GitHub credential.

Generate a strong webhook secret, enter it in the App's webhook settings, and inject the same value into BOSS. Keep the source-specific mapping so unrelated inbound providers do not share it:

```dotenv
BOSS_GITHUB_WEBHOOK_SECRET=replace-with-a-random-secret
BOSS_GITHUB_APP_BOT_LOGIN=boss[bot]
BOSS_WEBHOOK_INBOUND_SECRETS_JSON={"github":"env:BOSS_GITHUB_WEBHOOK_SECRET"}
BOSS_WEBHOOK_PROJECT_MAP_JSON={"owner/repository":"boss-dogfood"}
```

Configure the GitHub webhook URL as:

```text
https://boss.example/v1/webhooks/github
```

The generic `BOSS_WEBHOOK_INBOUND_SECRET` remains available for a single-source deployment, but a source-specific `github` entry is preferred. When neither secret is set, the receiver returns HTTP 503 without parsing or storing the payload. When the App is not installed and no legacy PAT is configured, outbound GitHub calls fail closed with a configuration error. This leaves community automation inert without crashing the API.

Valid deliveries are stored in `inbound_webhooks` and normalized to typed `github.issue_opened`, `github.issue_comment`, `github.pr_opened`, or `github.pr_review` events. Event subscriptions enqueue leased `github.triage` and `github.pr_review` jobs; no model or provider call runs in the webhook request. A repeated `X-GitHub-Delivery` creates neither a second event nor a second job. Unsupported event actions and messages sent by `BOSS_GITHUB_APP_BOT_LOGIN` return HTTP 204 without agent work. An unmapped repository also launches no work; a missing or invalid signature returns HTTP 401 and records only rejection metadata, never the unverified payload.

Issue triage uses the shared orchestration intent classifier, posts exactly one accept/decline/needs-info verdict, and creates a hold-default decision. An accepted request creates only a proposed plan; approving its decision dispatches phase tasks. PR review reads the unified diff without checking out or executing fork code, posts `COMMENT`-only inline findings plus a value/ROI note addressed to the author, and creates a hold-default merge-authorization decision. The agent never calls merge. Every GitHub read/write is represented by a Receipt and deduplicated by delivery or PR head SHA.

The default public review path is deterministic and does not incur model cost. To add the existing read-only code-review model role, opt in explicitly; the call is budget-reserved and Receipt-backed, and safely falls back to deterministic review if denied or unavailable:

```dotenv
BOSS_GITHUB_COMMUNITY_MODEL_REVIEW_ENABLED=true
BOSS_GITHUB_COMMUNITY_MODEL_REVIEW_BUDGET_USD_MICROS=250000
```

The older `/v1/webhooks/inbound/github` route remains available for generic provider integrations. Use `/v1/webhooks/github` for the strict community action allowlist and 204 acknowledgements.

## Observe and recover

- `GET /v1/webhook-deliveries?project_id=...` lists pending, delivered, failed, retrying, and deadlettered deliveries.
- `GET /v1/events?project_id=...` shows endpoint, delivery, inbound, rejection, and unrouted audit events.
- `GET /v1/deadletters?project_id=...` shows terminal worker failures and supports the existing remediation workflow.
- Webhook job deadletters include `related_entity_type=webhook_delivery`, the
  durable delivery ID, and its current status so an operator can move directly
  from the shared deadletter feed to the delivery record.

Run the invariant suite after changing subscriptions or webhook configuration:

```bash
curl -X POST "$BOSS_URL/v1/canaries/run" \
  -H "Authorization: Bearer $BOSS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"boss-dogfood","actor":"operator"}'
```

The `reactive_dispatch_exactly_once` result proves that one dispatchable event
creates one job for its enabled subscription, ignores a disabled subscription,
reuses the same job when the event is replayed, and completes it through the
normal queued→leased→succeeded protocol. Results persist in `canary_runs` and
are available from `GET /v1/canaries?project_id=...`.

HTTP 2xx marks a delivery complete. Non-2xx responses and timeouts use bounded exponential job backoff and transition to a deadletter after `max_attempts`; BOSS does not run a hidden retry loop.

## Optional Go fan-out sidecar (BOSS-102 / S3) — ◐ not GA

Community delivery is the Python path above. A smallest Go dispatcher exists
in-repo at `services/webhook-dispatcher` for high-fanout *I/O only*. It does
not own events, subscriptions, or the ledger.

- Unset `BOSS_GO_WEBHOOK_DISPATCHER_URL` → the Python adapter returns
  `not_configured` and the engine keeps working. This is the default.
- When set, the adapter is **enqueue-only**: it POSTs the already-built event
  envelope to `POST /v1/dispatch`. It never calls destination webhook URLs.
- The sidecar HMAC-signs each destination (`X-Hub-Signature-256`), retries,
  then deadletters. Every destination gets a Receipt + unique `dedupe_key`.
- `BOSS_GO_WEBHOOK_DISPATCHER_DRY_RUN` defaults to `true` and never opens
  outbound sockets. Do not send real Slack/email through this sidecar.
- `s3_go_event_dispatcher` stays **false** until a live probe exists.
  In-repo source is not a live mount. Not Community GA.
  See [`GO_SCALE_TRACK.md`](GO_SCALE_TRACK.md).
