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

## Configure GitHub inbound delivery

Configure the GitHub webhook URL as:

```text
https://boss.example/v1/webhooks/inbound/github
```

Use the same secret as `BOSS_WEBHOOK_INBOUND_SECRET` or the `github` entry in `BOSS_WEBHOOK_INBOUND_SECRETS_JSON`. Subscribe GitHub to `pull_request`, `check_suite`, and `issue_comment` events.

Valid deliveries are stored in `inbound_webhooks`, normalized to `webhook.inbound.received`, and routed through the normal event subscriptions. A repeated `X-GitHub-Delivery` is a no-op. An unmapped repository returns HTTP 202 and records `webhook.inbound.unrouted`; a missing or invalid signature returns HTTP 401 and records only rejection metadata, never the unverified payload.

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
