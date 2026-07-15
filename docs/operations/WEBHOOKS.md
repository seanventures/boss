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

Endpoint mutations and manual queueing require the project actor's `comms_manage` permission. The execute endpoint is worker-only: it validates the worker ID, active lease token, lease expiry, job kind, project, and delivery ownership before making any external call.

The receiver gets canonical JSON and these headers:

- `X-Boss-Event`: canonical event type
- `X-Boss-Delivery`: durable BOSS delivery ID
- `X-Hub-Signature-256`: `sha256=` plus the HMAC-SHA256 of the exact request bytes

Only endpoints whose `data_boundary` is at least as restrictive as the event may receive it.

## Enable the GitHub App community surface

Use [`.github/boss-app-manifest.json`](../../.github/boss-app-manifest.json) as the App-manifest template. Before submitting it through GitHub's App setup flow, replace `boss.example` with the public HTTPS host of the BOSS API. Install the resulting App on only the repositories BOSS should manage, then record its App id, installation id, and generated private key:

```dotenv
BOSS_GITHUB_APP_ID=123456
BOSS_GITHUB_APP_INSTALLATION_ID=789012
BOSS_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/boss-github-app.pem
```

The manifest requests `contents: write`, `issues: write`, and `pull_requests: write`. It subscribes to `issues`, `issue_comment`, `pull_request`, and `pull_request_review`. BOSS narrows each installation token to one configured repository and those permissions. Community replies and reviews use the same short-lived installation-token path as existing PR creation; there is no second GitHub credential.

Generate a strong webhook secret, enter it in the App's webhook settings, and inject the same value into BOSS. Keep the source-specific mapping so unrelated inbound providers do not share it:

```dotenv
BOSS_GITHUB_WEBHOOK_SECRET=replace-with-a-random-secret
BOSS_WEBHOOK_INBOUND_SECRETS_JSON={"github":"env:BOSS_GITHUB_WEBHOOK_SECRET"}
BOSS_WEBHOOK_PROJECT_MAP_JSON={"owner/repository":"boss-dogfood"}
```

Configure the GitHub webhook URL as:

```text
https://boss.example/v1/webhooks/inbound/github
```

The generic `BOSS_WEBHOOK_INBOUND_SECRET` remains available for a single-source deployment, but a source-specific `github` entry is preferred. When neither secret is set, the receiver returns HTTP 503 without parsing or storing the payload. When the App is not installed and no legacy PAT is configured, outbound GitHub calls fail closed with a configuration error. This leaves community automation inert without crashing the API.

Valid deliveries are stored in `inbound_webhooks`, normalized to `webhook.inbound.received`, and routed through the normal event subscriptions. A repeated `X-GitHub-Delivery` is a no-op. An unmapped repository returns HTTP 202 and records `webhook.inbound.unrouted`; a missing or invalid signature returns HTTP 401 and records only rejection metadata, never the unverified payload.

The current receiver routes `pull_request`, `check_suite`, and `issue_comment`. The manifest also subscribes to `issues` and `pull_request_review` so those deliveries can be enabled by the planned community triage and review consumers without reinstalling the App; until those consumers land, unsupported deliveries are acknowledged as unrouted and do not launch agent work.

## Observe and recover

- `GET /v1/webhook-deliveries?project_id=...` lists pending, delivered, failed, retrying, and deadlettered deliveries.
- `GET /v1/events?project_id=...` shows endpoint, delivery, inbound, rejection, and unrouted audit events.
- `GET /v1/deadletters?project_id=...` shows terminal worker failures and supports the existing remediation workflow.

HTTP 2xx marks a delivery complete. Non-2xx responses and timeouts use bounded exponential job backoff and transition to a deadletter after `max_attempts`; BOSS does not run a hidden retry loop.
